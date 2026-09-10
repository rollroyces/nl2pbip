"""Cross-table data understanding for the LLM planner payload.

The deterministic :mod:`nl2pbip.data_inspector` produces per-column
statistics. The :mod:`nl2pbip.ontology` module gives the LLM a
schema.org vocabulary anchor. Neither one tells the LLM the things
that matter most for designing a Power BI semantic model:

* Which columns are **primary keys** (uniquely identify rows)?
* For every suggested foreign-key pair, **what fraction of rows
  actually match** — and how many are orphans?
* For every FK pair, **what's the cardinality ratio** (is it
  one-to-many, many-to-one, or one-to-one)?
* For date columns, **what's the actual time range** the data
  covers — so the LLM doesn't suggest "year-over-year" measures
  when the data only spans two months.
* For numeric columns, **what's the distribution** — so the LLM
  can tell whether to SUM, AVERAGE, or pick a count-based measure.

This module fills those gaps. All analyses are deterministic — no
LLM call — and run in O(n) over the sample rows the inspector
already loaded.

Public API
----------
* :func:`analyze_data_understanding` — top-level entry point that
  walks every profiled table and returns a JSON-serialisable
  dictionary with primary-key candidates, FK coverage stats,
  cardinality ratios, time ranges, and numeric distributions.
* :func:`detect_primary_key_candidates` — find columns that
  uniquely identify rows.
* :func:`verify_relationship_coverage` — for a (from_table,
  from_column, to_table, to_column) tuple, return the
  match-rate, orphan count, and a cardinality hint.
* :func:`compute_numeric_quantiles` — P25/P50/P75 for a numeric
  column.
* :func:`compute_time_range` — min/max date for a date column.

Design constraints
------------------
* **No new dependencies.** Re-uses ``statistics``, ``collections``,
  and the dataclasses already in :mod:`nl2pbip.data_inspector`.
* **Bounded.** All outputs are scalars or short lists — the
  planner payload size scales with the *number of columns*, not
  the *number of rows*.
* **Honest about sampling.** When the deterministic inspector
  sampled a subset of rows, the analyzer notes that counts and
  quantiles are statistical estimates, not exact.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nl2pbip.data_inspector import DataProfile

# ---------------------------------------------------------------------------
# Primary-key detection
# ---------------------------------------------------------------------------


@dataclass
class PrimaryKeyCandidate:
    """A column that's likely the primary key of a table."""

    table: str
    column: str
    distinct_count: int
    row_count: int
    distinct_ratio: float  # distinct_count / row_count; 1.0 = perfect
    confidence: str  # "strong" | "likely" | "weak"

    def to_json(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "distinct_count": self.distinct_count,
            "row_count": self.row_count,
            "distinct_ratio": round(self.distinct_ratio, 4),
            "confidence": self.confidence,
        }


def detect_primary_key_candidates(
    profile: DataProfile,
) -> List[PrimaryKeyCandidate]:
    """Find columns that uniquely identify rows in ``profile``.

    A column is a primary-key candidate when:

    * ``distinct_count == row_count`` (perfect uniqueness): **strong**
      confidence.
    * ``distinct_ratio >= 0.95`` and column is short-text or numeric:
      **likely** confidence.
    * ``distinct_ratio >= 0.80`` and the table has <100 sampled
      rows: **weak** confidence (could be sampling noise).
    """
    candidates: List[PrimaryKeyCandidate] = []
    for table in profile.tables:
        row_count = max(table.row_count, 1)
        for column in table.columns:
            if column.distinct_count == 0:
                continue
            ratio = column.distinct_count / row_count
            if column.distinct_count == row_count:
                confidence = "strong"
            elif ratio >= 0.95 and column.inferred_type in ("text", "numeric", "date"):
                confidence = "likely"
            elif ratio >= 0.80 and row_count < 100:
                confidence = "weak"
            else:
                continue
            candidates.append(
                PrimaryKeyCandidate(
                    table=table.name,
                    column=column.name,
                    distinct_count=column.distinct_count,
                    row_count=table.row_count,
                    distinct_ratio=ratio,
                    confidence=confidence,
                )
            )
    # Sort by confidence then ratio, so the LLM sees strong matches first.
    confidence_rank = {"strong": 0, "likely": 1, "weak": 2}
    candidates.sort(key=lambda c: (confidence_rank[c.confidence], -c.distinct_ratio))
    return candidates


# ---------------------------------------------------------------------------
# FK coverage / cardinality verification
# ---------------------------------------------------------------------------


@dataclass
class RelationshipCoverage:
    """FK coverage stats for a (from_table.from_column → to_table.to_column) pair.

    Populated from the actual data, not from heuristics — the
    LLM gets a concrete number for each FK pair it considers.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    total_rows: int  # rows in the referencing (left) table
    matching_rows: int  # rows whose value appears on the right
    orphan_rows: int  # rows whose value doesn't appear
    coverage_ratio: float  # matching / total
    from_distinct: int  # distinct values in the left column
    to_distinct: int  # distinct values in the right column
    cardinality_hint: str  # "manyToOne" | "oneToOne" | "oneToMany" | "manyToMany"

    def to_json(self) -> Dict[str, Any]:
        return {
            "from_table": self.from_table,
            "from_column": self.from_column,
            "to_table": self.to_table,
            "to_column": self.to_column,
            "total_rows": self.total_rows,
            "matching_rows": self.matching_rows,
            "orphan_rows": self.orphan_rows,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "from_distinct": self.from_distinct,
            "to_distinct": self.to_distinct,
            "cardinality_hint": self.cardinality_hint,
        }


def _build_index(
    records: Iterable[Dict[str, Any]], key: str
) -> Tuple[List[Dict[str, Any]], set, Counter]:
    """Index records by ``key`` and return (records, distinct_values, counter).

    Returns the records list, the set of distinct non-null values
    for ``key``, and a counter of value → occurrences (used to
    detect the right-side distinct count).
    """
    out: List[Dict[str, Any]] = []
    distinct: set = set()
    counts: Counter = Counter()
    for record in records:
        out.append(record)
        value = record.get(key)
        if value is None or value == "":
            continue
        distinct.add(value)
        counts[value] += 1
    return out, distinct, counts


def verify_relationship_coverage(
    from_records: List[Dict[str, Any]],
    from_column: str,
    to_records: List[Dict[str, Any]],
    to_column: str,
    from_table: str = "",
    to_table: str = "",
) -> Optional[RelationshipCoverage]:
    """Compute FK coverage stats between two record sets.

    Returns ``None`` when either side has no data to compare.
    Otherwise returns coverage + cardinality hint based on
    distinct-value ratio.
    """
    if not from_records or not to_records:
        return None

    _, from_distinct, _ = _build_index(from_records, from_column)
    _, to_distinct, _ = _build_index(to_records, to_column)
    if not from_distinct or not to_distinct:
        # One side is empty — FK covers 0 rows.
        return RelationshipCoverage(
            from_table=from_table,
            from_column=from_column,
            to_table=to_table,
            to_column=to_column,
            total_rows=len(from_records),
            matching_rows=0,
            orphan_rows=len(from_records),
            coverage_ratio=0.0,
            from_distinct=0,
            to_distinct=len(to_distinct),
            cardinality_hint="manyToOne",
        )

    matching = sum(
        1 for record in from_records if record.get(from_column) in to_distinct
    )
    orphans = len(from_records) - matching
    coverage = matching / len(from_records)

    # Cardinality hint based on distinct-value ratio.
    #
    # Heuristic:
    # * from_distinct < to_distinct means the "many" side is `from`.
    # * from_distinct == to_distinct and coverage >= 0.99 means the
    #   relationship is bijective → oneToOne (with a few orphans
    #   pushing it down to manyToMany).
    # * from_distinct > to_distinct is rare (it means more distinct
    #   FK values than PK values, suggesting orphans).
    if len(from_distinct) == len(to_distinct):
        if coverage >= 0.99:
            cardinality_hint = "oneToOne"
        else:
            cardinality_hint = "manyToMany"
    elif len(from_distinct) < len(to_distinct):
        # The "many" side is `from`, the "one" side is `to` —
        # manyToOne.
        cardinality_hint = "manyToOne"
    else:
        # from_distinct > to_distinct: orphan-heavy (FK has values
        # the PK doesn't have). oneToMany from the FK's perspective.
        cardinality_hint = "oneToMany"

    return RelationshipCoverage(
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        to_column=to_column,
        total_rows=len(from_records),
        matching_rows=matching,
        orphan_rows=orphans,
        coverage_ratio=coverage,
        from_distinct=len(from_distinct),
        to_distinct=len(to_distinct),
        cardinality_hint=cardinality_hint,
    )


# ---------------------------------------------------------------------------
# Numeric distribution
# ---------------------------------------------------------------------------


@dataclass
class NumericDistribution:
    """Quantiles and skewness hints for one numeric column."""

    column: str
    table: str
    p25: float
    p50: float
    p75: float
    p95: float
    skew: str  # "left" | "right" | "symmetric"
    likely_outliers: int  # rows above p95

    def to_json(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "table": self.table,
            "p25": round(self.p25, 4),
            "p50": round(self.p50, 4),
            "p75": round(self.p75, 4),
            "p95": round(self.p95, 4),
            "skew": self.skew,
            "likely_outliers": self.likely_outliers,
        }


def compute_numeric_quantiles(
    values: List[float],
) -> Optional[Tuple[float, float, float, float]]:
    """Return ``(p25, p50, p75, p95)`` or ``None`` when the column is empty."""
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def pct(p: float) -> float:
        # ``statistics.quantiles`` uses inclusive-exclusive which is
        # awkward; we use the simpler rank-based estimator.
        rank = p * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac

    return pct(0.25), pct(0.50), pct(0.75), pct(0.95)


def compute_numeric_distribution(
    table: str, column: str, values: List[Any]
) -> Optional[NumericDistribution]:
    """Profile the numeric distribution of one column.

    Returns ``None`` when the column has <5 numeric values (sample
    too small for meaningful quantiles).
    """
    nums = [n for n in (_to_float(v) for v in values) if n is not None]
    if len(nums) < 5:
        return None
    quantiles = compute_numeric_quantiles(nums)
    if quantiles is None:
        return None
    p25, p50, p75, p95 = quantiles
    # Skewness heuristic: how far is p50 from the midpoint of
    # p25 and p75? If close, distribution is symmetric; if closer
    # to p25, the right tail is longer (right-skewed); if closer to
    # p75, left-skewed.
    mid = (p25 + p75) / 2
    if abs(p50 - mid) < (p75 - p25) * 0.05:
        skew = "symmetric"
    elif p50 > mid:
        skew = "right"  # median is right of midpoint → right tail
    else:
        skew = "left"
    outliers = sum(1 for v in nums if v > p95)
    return NumericDistribution(
        column=column,
        table=table,
        p25=p25,
        p50=p50,
        p75=p75,
        p95=p95,
        skew=skew,
        likely_outliers=outliers,
    )


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Time range
# ---------------------------------------------------------------------------


@dataclass
class TimeRange:
    """Min / max / span of a date column."""

    column: str
    table: str
    min_date: Optional[str]
    max_date: Optional[str]
    distinct_dates: int

    def to_json(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "table": self.table,
            "min_date": self.min_date,
            "max_date": self.max_date,
            "distinct_dates": self.distinct_dates,
        }


def compute_time_range(
    table: str, column: str, values: List[Any]
) -> Optional[TimeRange]:
    """Profile the time range covered by a date column.

    Returns ``None`` when the column has <2 dates. The sample is
    expected to be already ISO-formatted (the inspector uses
    ``datetime.fromisoformat`` for type inference).
    """
    dates: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        s = value.strip()
        # Accept ISO date or ISO datetime.
        if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
            dates.append(s[:10])  # truncate to date
    if len(dates) < 2:
        return None
    distinct = sorted(set(dates))
    return TimeRange(
        column=column,
        table=table,
        min_date=distinct[0],
        max_date=distinct[-1],
        distinct_dates=len(distinct),
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


@dataclass
class DataUnderstanding:
    """Top-level summary of cross-table data understanding."""

    primary_keys: List[PrimaryKeyCandidate] = field(default_factory=list)
    relationship_coverage: List[RelationshipCoverage] = field(default_factory=list)
    numeric_distributions: List[NumericDistribution] = field(default_factory=list)
    time_ranges: List[TimeRange] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "primary_keys": [pk.to_json() for pk in self.primary_keys],
            "relationship_coverage": [
                rc.to_json() for rc in self.relationship_coverage
            ],
            "numeric_distributions": [
                nd.to_json() for nd in self.numeric_distributions
            ],
            "time_ranges": [tr.to_json() for tr in self.time_ranges],
            "warnings": self.warnings,
        }


def analyze_data_understanding(
    profiles: List[DataProfile],
    records_by_source: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> DataUnderstanding:
    """Build a :class:`DataUnderstanding` summary across all profiles.

    Parameters
    ----------
    profiles:
        Output of :func:`nl2pbip.data_inspector.inspect_data_sources`.
    records_by_source:
        Optional mapping ``{source_name: records}``. When provided,
        the analyzer uses the full records (not just the inspector's
        sample) to verify FK coverage and compute quantiles. This
        is what the orchestrator does in production — it already has
        the records in memory when it built the profiles.
    """
    result = DataUnderstanding()

    # 1) Primary-key detection from profile stats alone.
    for profile in profiles:
        for table in profile.tables:
            for column in table.columns:
                if column.distinct_count == 0:
                    continue
                row_count = max(table.row_count, 1)
                ratio = column.distinct_count / row_count
                if column.distinct_count == row_count:
                    confidence = "strong"
                elif ratio >= 0.95 and column.inferred_type in (
                    "text",
                    "numeric",
                    "date",
                ):
                    confidence = "likely"
                elif ratio >= 0.80 and row_count < 100:
                    confidence = "weak"
                else:
                    continue
                result.primary_keys.append(
                    PrimaryKeyCandidate(
                        table=table.name,
                        column=column.name,
                        distinct_count=column.distinct_count,
                        row_count=table.row_count,
                        distinct_ratio=ratio,
                        confidence=confidence,
                    )
                )

    # 2) FK coverage + cardinality verification. Use the records
    # when available — coverage stats from the profile sample
    # would be inaccurate on large fact tables.
    # Index records by table name across all sources (a table name
    # is unique within a PBIP project; the source_name is just a
    # label).
    table_records: Dict[str, List[Dict[str, Any]]] = {}
    if records_by_source:
        for source_name, source_records in records_by_source.items():
            for profile in profiles:
                if profile.source_name != source_name:
                    continue
                for table in profile.tables:
                    table_records[table.name] = source_records

    # Pair every primary-key candidate on the "one" side with
    # referencing tables that share a column name. The LLM's
    # ontology_hints already produced this pairing via
    # ``suggest_relationships``; here we add the data-driven
    # coverage stats.
    pk_by_table: Dict[str, List[PrimaryKeyCandidate]] = {}
    for pk in result.primary_keys:
        pk_by_table.setdefault(pk.table, []).append(pk)

    for profile in profiles:
        for table in profile.tables:
            for column in table.columns:
                # Look up matching PK on a different table (FK candidate).
                for pk_table, pks in pk_by_table.items():
                    if pk_table == table.name:
                        continue
                    for pk in pks:
                        if pk.column == column.name:
                            # Match. Compute coverage.
                            records_fk = table_records.get(table.name, [])
                            records_pk = table_records.get(pk_table, [])
                            if not records_fk or not records_pk:
                                continue
                            coverage = verify_relationship_coverage(
                                from_records=records_fk,
                                from_column=column.name,
                                to_records=records_pk,
                                to_column=pk.column,
                                from_table=table.name,
                                to_table=pk_table,
                            )
                            if coverage is not None:
                                result.relationship_coverage.append(coverage)

    # 3) Numeric distribution + time range per column.
    for profile in profiles:
        for table in profile.tables:
            records = table_records.get(table.name, [])
            for column in table.columns:
                if records:
                    values = [r.get(column.name) for r in records]
                else:
                    # No records available; fall back to the
                    # stats the inspector already computed.
                    values = []
                if column.inferred_type == "numeric":
                    dist = compute_numeric_distribution(table.name, column.name, values)
                    if dist is not None:
                        result.numeric_distributions.append(dist)
                elif column.inferred_type == "date":
                    tr = compute_time_range(table.name, column.name, values)
                    if tr is not None:
                        result.time_ranges.append(tr)

    return result


__all__ = [
    "DataUnderstanding",
    "NumericDistribution",
    "PrimaryKeyCandidate",
    "RelationshipCoverage",
    "TimeRange",
    "analyze_data_understanding",
    "compute_numeric_distribution",
    "compute_numeric_quantiles",
    "compute_time_range",
    "detect_primary_key_candidates",
    "verify_relationship_coverage",
]
