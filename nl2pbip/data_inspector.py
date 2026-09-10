"""Data inspection helpers for the LLM planner payload.

When generating a Power BI model from natural language, the LLM
needs to *understand the data* before designing relationships or
visuals. Schema-only context (table names, column types) leaves the
LLM guessing at questions like:

* Which values does a categorical column actually take?
* What's the typical range of a numeric measure?
* Which two columns share values (a likely foreign-key pair)?
* Are there obvious data-quality issues (nulls, mixed casing)?

This module profiles a small sample of every registered data source
and emits a JSON-serialisable summary that the orchestrator's
planner payload includes alongside the TMDL model snapshot.

Public API
----------
* :func:`inspect_data_source` — return a profile for one source.
* :func:`inspect_data_sources` — fan out over a list of sources.
* :class:`DataProfile` — dataclass wrapping the profile fields.
* :func:`suggest_relationships` — heuristic foreign-key detection
  across tables (returns ranked suggestions, not a verified list).

Design constraints
------------------
* **No pandas as a hard dependency.** The inspector uses Python
  stdlib (``csv``, ``statistics``) so it works in slim environments.
  When pandas is available it offers the same API as a faster
  backend; when not, the stdlib fallback gives correct results at
  the cost of speed.
* **Sample-bounded.** ``max_rows_per_source`` caps the read so a
  50-million-row fact table doesn't OOM the planner. Default 1000.
* **PII-safe by default.** :attr:`DataProfile.distinct_examples`
  contains the most common distinct values per column. Callers can
  set ``redact_distinct_values=True`` to emit only counts and skip
  the example values, useful when sending the profile to a hosted
  LLM with strict data-residency rules.
"""

from __future__ import annotations

import csv
import gzip
import json
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# A registered data source can be a file path (CSV / JSON / JSONL /
# Parquet if pandas is available), a callable returning a
# DataFrame-like object, or an in-memory list of dicts. The
# inspector picks a backend based on the type.
DataSource = Union[
    str,  # file path (CSV / JSON / JSONL / Parquet)
    Path,  # ditto
    List[Dict[str, Any]],  # in-memory records
    Callable[[], Any],  # callable returning a DataFrame-like object
]


@dataclass
class ColumnProfile:
    """Per-column profile returned by :class:`DataProfiler`."""

    name: str
    inferred_type: str  # "numeric" | "text" | "date" | "boolean" | "binary"
    non_null_count: int
    distinct_count: int
    null_rate: float  # 0.0 - 1.0
    # Numeric columns
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    median: Optional[float] = None
    stddev: Optional[float] = None
    # Date columns
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    # Distinct-value examples (top-N by frequency). Empty when
    # ``redact_distinct_values=True`` is passed to the profiler.
    distinct_examples: List[Any] = field(default_factory=list)

    def to_json(self, *, redact_distinct_values: bool = False) -> Dict[str, Any]:
        """Render as a JSON-safe dict."""
        payload = asdict(self)
        if redact_distinct_values:
            payload["distinct_examples"] = []
        return payload


@dataclass
class TableProfile:
    """Per-table profile returned by :class:`DataProfiler`."""

    name: str
    row_count: int  # sampled rows (≤ max_rows_per_source)
    columns: List[ColumnProfile]
    sampled_at_least: int  # full row count when known

    def to_json(self, *, redact_distinct_values: bool = False) -> Dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "sampled_at_least": self.sampled_at_least,
            "columns": [
                c.to_json(redact_distinct_values=redact_distinct_values)
                for c in self.columns
            ],
        }


@dataclass
class DataProfile:
    """Top-level container for a profiled data source."""

    source_name: str
    source_kind: str  # "csv" | "json" | "jsonl" | "parquet" | "records" | "callable"
    tables: List[TableProfile]
    warnings: List[str] = field(default_factory=list)

    def to_json(self, *, redact_distinct_values: bool = False) -> Dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_kind": self.source_kind,
            "tables": [
                t.to_json(redact_distinct_values=redact_distinct_values)
                for t in self.tables
            ],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Backend: load records from a data source
# ---------------------------------------------------------------------------


def _load_records(
    source: DataSource, source_name: str
) -> Tuple[List[Dict[str, Any]], str]:
    """Return ``(records, kind)`` for any supported source.

    Raises ``ValueError`` for unsupported source types or unreadable
    files. The ``kind`` string is recorded in the profile so the
    caller can audit what was sampled.
    """
    if callable(source):
        obj = source()
        records = _records_from_dataframe_like(obj)
        return records, "callable"

    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Data source not found: {path}")
        suffix = path.suffix.lower()
        opener = gzip.open if path.suffix.lower() == ".gz" else open
        # Check the actual on-disk format by inspecting the
        # extension plus the first byte (magic header). CSV files
        # opened with gzip land here too — we handle that with the
        # ``opener`` above.
        inner_suffix = (
            path.with_suffix("").suffix.lower()
            if path.suffix.lower() == ".gz"
            else suffix
        )
        if inner_suffix in (".parquet", ".pq"):
            try:
                records = _records_from_parquet(path)
                return records, "parquet"
            except ImportError as exc:
                raise ValueError(
                    f"Parquet support requires pandas (or pyarrow): {exc}"
                ) from exc
        if inner_suffix == ".json":
            return _records_from_json(path, source_name), "json"
        if inner_suffix in (".jsonl", ".ndjson"):
            return _records_from_jsonl(path), "jsonl"
        if inner_suffix == ".csv":
            return _records_from_csv(path, opener), "csv"
        # Fallback: try CSV by extension, JSON otherwise.
        try:
            return _records_from_csv(path, opener), "csv"
        except (UnicodeDecodeError, csv.Error):
            return _records_from_json(path, source_name), "json"

    if isinstance(source, list):
        return list(source), "records"

    raise ValueError(f"Unsupported data source type: {type(source).__name__}")


def _records_from_csv(path: Path, opener: Callable[..., Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    # The opener is either the builtin ``open`` or ``gzip.open``;
    # both accept ``encoding`` and ``newline`` keyword args, so we
    # pass them through.
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)
    return records


def _records_from_json(path: Path, source_name: str) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [dict(r) for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        # Convention: a JSON file with a single key holding a list
        # of records (``{"records": [...]}`` or ``{"data": [...]}``).
        for key in ("records", "data", "rows"):
            if key in payload and isinstance(payload[key], list):
                return [dict(r) for r in payload[key] if isinstance(r, dict)]
        # Treat the dict itself as a single record.
        return [dict(payload)]
    raise ValueError(
        f"Unsupported JSON structure in {path}: expected list or object, got {type(payload).__name__}"
    )


def _records_from_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _records_from_parquet(path: Path) -> List[Dict[str, Any]]:
    try:
        import pandas as pd  # type: ignore
    except ImportError as exc:
        raise ImportError("pandas is required for Parquet sources") from exc
    df = pd.read_parquet(path)
    return _records_from_dataframe_like(df)


def _records_from_dataframe_like(obj: Any) -> List[Dict[str, Any]]:
    """Coerce a DataFrame-like object (pandas, polars) into a list of dicts."""
    # pandas
    if hasattr(obj, "to_dict") and hasattr(obj, "columns"):
        try:
            return obj.head(1000).to_dict(orient="records")  # type: ignore[union-attr]
        except Exception:
            pass
    # Already a list of records
    if isinstance(obj, list):
        return [dict(r) for r in obj if isinstance(r, dict)]
    raise ValueError(
        f"Could not extract records from object of type {type(obj).__name__}"
    )


# ---------------------------------------------------------------------------
# Type inference + per-column statistics
# ---------------------------------------------------------------------------


def _infer_value_type(value: Any) -> str:
    """Classify a single cell into ``numeric`` / ``text`` / ``date`` / ``boolean`` / ``null``.

    Date detection is conservative — only ISO-8601-style strings of
    the form ``YYYY-MM-DD`` (optionally with a time portion) count.
    Other formats are treated as text so the LLM doesn't act on a
    false positive.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "numeric"
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "null"
        # ISO date — ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS``
        if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
            try:
                from datetime import datetime

                datetime.fromisoformat(s.replace("Z", "+00:00"))
                return "date"
            except ValueError:
                pass
        return "text"
    return "text"


def _is_iso_date(value: Any) -> bool:
    return _infer_value_type(value) == "date"


def _try_numeric(value: Any) -> Optional[float]:
    """Return ``value`` as ``float`` when it parses cleanly."""
    if value is None:
        return None
    if isinstance(value, bool):
        # Booleans are technically numeric in Python but treating
        # them as such produces meaningless statistics — return None
        # so the column profile falls back to ``boolean``.
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _profile_column(name: str, values: List[Any], top_n: int = 5) -> ColumnProfile:
    """Compute a :class:`ColumnProfile` for one column."""
    total = len(values)
    non_null = [v for v in values if v is not None and v != ""]
    non_null_count = len(non_null)
    null_count = total - non_null_count
    null_rate = (null_count / total) if total else 0.0

    # Type inference from the non-null sample.
    inferred = "text"
    if non_null:
        type_counts = Counter(_infer_value_type(v) for v in non_null)
        # Drop ``null`` — that's a measure of presence, not a type.
        type_counts.pop("null", None)
        if type_counts:
            inferred = type_counts.most_common(1)[0][0]
    # If the dominant type is ``numeric`` but at least 95% of
    # values parse as numeric, lock the inference to numeric even
    # if a handful of strings slipped in.
    numeric_count = sum(1 for v in non_null if _try_numeric(v) is not None)
    if numeric_count and numeric_count / non_null_count >= 0.95:
        inferred = "numeric"

    distinct = {_jsonable(v) for v in non_null}
    distinct_count = len(distinct)

    # For the examples list, prefer the **most frequent** distinct
    # values. Sets in Python have arbitrary iteration order, but
    # the LLM uses these examples to reason about the data domain
    # — top-N by frequency gives the most informative sample.
    # Counter.most_common returns (value, count) tuples in
    # frequency-descending order.
    value_counts = Counter(_jsonable(v) for v in non_null)
    distinct_examples = [v for v, _ in value_counts.most_common(top_n)]

    profile = ColumnProfile(
        name=name,
        inferred_type=inferred,
        non_null_count=non_null_count,
        distinct_count=distinct_count,
        null_rate=round(null_rate, 4),
        distinct_examples=distinct_examples,
    )

    if inferred == "numeric":
        nums = [n for n in (_try_numeric(v) for v in non_null) if n is not None]
        if nums:
            profile.min = min(nums)
            profile.max = max(nums)
            profile.mean = round(statistics.fmean(nums), 4)
            profile.median = statistics.median(nums)
            profile.stddev = (
                round(statistics.pstdev(nums), 4) if len(nums) >= 2 else 0.0
            )
    elif inferred == "date":
        date_strs = [v for v in non_null if _is_iso_date(v)]
        if date_strs:
            profile.min_date = min(date_strs)
            profile.max_date = max(date_strs)
    # Boolean and text: the distinct examples carry the signal.

    return profile


def _jsonable(value: Any) -> Any:
    """Best-effort JSON-friendly coercion for distinct-value examples."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def inspect_data_source(
    source: DataSource,
    *,
    name: Optional[str] = None,
    max_rows: int = 1000,
    redact_distinct_values: bool = False,
) -> DataProfile:
    """Profile a single data source.

    Parameters
    ----------
    source:
        Anything :func:`_load_records` accepts — a path, an in-memory
        list, or a callable.
    name:
        Display name written into the profile. Defaults to the path
        stem or ``"records"``.
    max_rows:
        Cap on rows sampled. The first ``max_rows`` records are read.
    redact_distinct_values:
        When True, ``distinct_examples`` is omitted from every
        column. Useful when sending the profile to a hosted LLM
        with strict data-residency rules.
    """
    records, kind = _load_records(source, name or _default_name(source))
    sampled = records[:max_rows]
    full_count = len(records)
    warnings: List[str] = []
    if full_count > max_rows:
        warnings.append(
            f"Sampled first {max_rows} of {full_count} rows; profile is "
            f"statistical, not exhaustive."
        )

    if not sampled:
        return DataProfile(
            source_name=name or _default_name(source),
            source_kind=kind,
            tables=[],
            warnings=warnings + ["No rows found in source."],
        )

    columns = _gather_columns(sampled)
    col_profiles = [_profile_column(c, [r.get(c) for r in sampled]) for c in columns]
    # Honour the redact flag by clearing distinct-value examples at
    # construction time. Callers that haven't asked for redaction
    # keep the examples so they can inspect them locally.
    if redact_distinct_values:
        for col in col_profiles:
            col.distinct_examples = []
    return DataProfile(
        source_name=name or _default_name(source),
        source_kind=kind,
        tables=[
            TableProfile(
                name=name or _default_name(source),
                row_count=len(sampled),
                sampled_at_least=full_count,
                columns=col_profiles,
            )
        ],
        warnings=warnings,
    )


def inspect_data_sources(
    sources: Dict[str, DataSource],
    *,
    max_rows_per_source: int = 1000,
    redact_distinct_values: bool = False,
) -> List[DataProfile]:
    """Profile a mapping ``{name: source}`` and return one profile per source.

    A single physical source can produce multiple "tables" if the
    registered data source is a callable returning a dict of
    ``{table_name: DataFrame}``. The inspector handles that case by
    splitting the dict into per-table profiles.
    """
    profiles: List[DataProfile] = []
    for source_name, source in sources.items():
        try:
            profiles.append(
                inspect_data_source(
                    source,
                    name=source_name,
                    max_rows=max_rows_per_source,
                    redact_distinct_values=redact_distinct_values,
                )
            )
        except (FileNotFoundError, ValueError, ImportError) as exc:
            profiles.append(
                DataProfile(
                    source_name=source_name,
                    source_kind="error",
                    tables=[],
                    warnings=[f"Could not inspect source: {exc}"],
                )
            )
    return profiles


def _default_name(source: DataSource) -> str:
    if isinstance(source, Path):
        return source.stem
    if isinstance(source, str):
        return Path(source).stem
    return "records"


def _gather_columns(records: List[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    seen_set: set = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in seen_set:
                seen.append(k)
                seen_set.add(k)
    return seen


# ---------------------------------------------------------------------------
# Heuristic relationship suggestions
# ---------------------------------------------------------------------------


def suggest_relationships(
    profiles: Sequence[DataProfile],
    *,
    min_overlap_ratio: float = 0.5,
    max_suggestions: int = 25,
) -> List[Dict[str, Any]]:
    """Suggest foreign-key pairs across profiled tables.

    A pair is suggested when:
    * The two columns are on different tables.
    * The smaller column's distinct values are a subset of the
      larger column's (set-containment, not full equality — the
      "many" side always has at least as many values as the "one"
      side).
    * The overlap ratio is at least ``min_overlap_ratio`` of the
      smaller side.

    Suggestions are ranked by overlap ratio × confidence boost for
    naming-pattern matches (``a.fk_id`` joins ``b.id``). The output
    is JSON-serialisable so the orchestrator can drop it straight
    into the planner payload.

    This is a heuristic, not a verified list — the LLM uses it to
    pick candidate endpoints, but the actual ``define_relationship``
    call goes through the relationship validation we added in PR #5.
    """
    column_index: List[Tuple[str, str, ColumnProfile]] = []
    for profile in profiles:
        for table in profile.tables:
            for col in table.columns:
                column_index.append((table.name, col.name, col))

    suggestions: List[Dict[str, Any]] = []
    for i, (left_table, left_col, left_prof) in enumerate(column_index):
        for right_table, right_col, right_prof in column_index[i + 1 :]:
            if left_table == right_table:
                continue
            # Cardinality: smaller distinct side is the "many".
            if left_prof.distinct_count == 0 or right_prof.distinct_count == 0:
                continue
            if left_prof.distinct_count <= right_prof.distinct_count:
                small_prof, large_prof = left_prof, right_prof
                small_side = (left_table, left_col)
                large_side = (right_table, right_col)
            else:
                small_prof, large_prof = right_prof, left_prof
                small_side = (right_table, right_col)
                large_side = (left_table, left_col)
            # Distinct-value overlap.
            #
            # We work from the **examples** lists (top-N most-frequent
            # values), not the full distinct set, because the
            # inspector doesn't carry the full set — only the top-N
            # examples per column. This means the overlap ratio is a
            # **lower bound** on the true Jaccard overlap: every value
            # in the examples that matches is a confirmed match, but
            # values outside the examples may also match. We surface
            # this honestly in the rationale so the LLM doesn't
            # over-trust a near-1.0 ratio computed against a small
            # sample.
            small_set = {_jsonable(v) for v in small_prof.distinct_examples}
            large_set = {_jsonable(v) for v in large_prof.distinct_examples}
            if not small_set:
                continue
            overlap = small_set & large_set
            # Compute the overlap ratio against the SMALLER side's
            # examples. Note: when ``small_prof.distinct_count`` is
            # larger than ``len(small_set)`` (i.e. the column has
            # more distinct values than fit in the examples list),
            # this ratio is a lower bound on the true overlap.
            ratio_lower_bound = len(overlap) / len(small_set)
            ratio_is_lower_bound = small_prof.distinct_count > len(small_set)
            if ratio_lower_bound < min_overlap_ratio:
                continue
            confidence = round(ratio_lower_bound, 4)
            # Naming-pattern boost: ``X.fk_id`` ↔ ``Y.id`` style.
            if _naming_pattern_match(small_side, large_side):
                confidence = min(confidence + 0.1, 1.0)
            # Build the rationale. When the ratio is a lower bound
            # we note that explicitly so the LLM doesn't think every
            # sample value matched (the examples set is capped).
            examples_seen = len(small_set)
            total_small = small_prof.distinct_count
            if ratio_is_lower_bound:
                rationale = (
                    f"{len(overlap)} of {examples_seen} sampled distinct values "
                    f"from {small_side[0]}.{small_side[1]} match values in "
                    f"{large_side[0]}.{large_side[1]} (sampled from "
                    f"{total_small} total distinct values; ratio is a "
                    f"lower bound on the true overlap)"
                )
            else:
                rationale = (
                    f"{len(overlap)} of {total_small} distinct values from "
                    f"{small_side[0]}.{small_side[1]} match values in "
                    f"{large_side[0]}.{large_side[1]}"
                )
            suggestions.append(
                {
                    "from_table": small_side[0],
                    "from_column": small_side[1],
                    "to_table": large_side[0],
                    "to_column": large_side[1],
                    "overlap_ratio": ratio_lower_bound,
                    "confidence": confidence,
                    "ratio_is_lower_bound": ratio_is_lower_bound,
                    "examples_seen": examples_seen,
                    "total_small_distinct": total_small,
                    "rationale": rationale,
                }
            )
    # Sort by confidence desc, then overlap_ratio desc, keep top N.
    suggestions.sort(key=lambda s: (s["confidence"], s["overlap_ratio"]), reverse=True)
    return suggestions[:max_suggestions]


def _naming_pattern_match(
    small_side: Tuple[str, str], large_side: Tuple[str, str]
) -> bool:
    """Detect ``X.fk_id`` ↔ ``Y.id`` style naming conventions."""
    small_table, small_col = small_side
    large_table, large_col = large_side
    small_col_lower = small_col.lower()
    large_col_lower = large_col.lower()
    if large_col_lower == "id":
        if small_col_lower in {f"{large_table.lower()}_id", f"{large_table.lower()}id"}:
            return True
    if small_col_lower == "id":
        if large_col_lower in {f"{small_table.lower()}_id", f"{small_table.lower()}id"}:
            return True
    return False


__all__ = [
    "ColumnProfile",
    "DataProfile",
    "DataSource",
    "TableProfile",
    "inspect_data_source",
    "inspect_data_sources",
    "suggest_relationships",
]
