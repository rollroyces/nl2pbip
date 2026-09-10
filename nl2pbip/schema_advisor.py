"""AI-assisted schema advisor for the LLM planner payload.

The deterministic :mod:`nl2pbip.data_inspector` produces accurate
statistics — column types, distinct counts, sample values,
overlap-based relationship suggestions. What it can't do is reason
about **semantics**:

* A column called ``x`` whose top-5 examples are ``EMEA, APAC,
  AMER, LATAM`` is clearly a region dimension — but the
  deterministic profile doesn't say so.
* A numeric column ``y`` ranging 1–9800 in a ``Sales`` table is
  almost certainly a measure (``Amount``, ``Revenue``, ``Price``)
  — but the LLM has to guess the aggregation from context alone.
* Two columns named ``cust_id`` and ``customer`` are obviously
  the same key, but the deterministic overlap check might miss
  it if the samples are sparse.

This module wraps the orchestrator's existing LLM client and asks
it to enrich the deterministic profile with three pieces of
context the LLM is uniquely positioned to produce:

1. **Column semantics** — for every profiled column, suggest a
   human-readable description and a "role" tag (dimension /
   measure / identifier / date). The role drives measure
   suggestions and visual choice.
2. **Measure suggestions** — for every numeric column in a
   table that has at least one categorical column, suggest
   ``SUM`` / ``AVERAGE`` / ``COUNT DISTINCT`` measures with a
   human-readable name.
3. **Visual suggestions** — for each measure/dimension pair,
   suggest the most natural visual type (``barChart`` for
   categorical × measure, ``lineChart`` for date × measure,
   ``card`` for measure alone).

Design constraints
------------------

* **Optional.** The advisor only runs when ``data_sources`` is
  registered and the orchestrator has an LLM client. Callers
  that want the deterministic profile only can opt out via
  ``context[\"data_inspector_ai_enabled\"] = False``.
* **Token-bounded.** The advisor sends the deterministic
  profile (table names, column types, top-5 examples, distinct
  counts, suggested relationships) as JSON. The full data is
  never sent — only the profile. This keeps the advisor's input
  size proportional to the number of *columns*, not the number
  of *rows*.
* **Graceful fallback.** If the LLM call fails (network error,
  parse error, timeout), the advisor returns ``None`` so the
  orchestrator can fall back to the deterministic profile
  unchanged.
* **Cached by prompt shape.** A small in-memory cache keyed by
  the deterministic profile's structural fingerprint prevents
  re-prompting the LLM when the orchestrator retries a plan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from nl2pbip.data_inspector import DataProfile

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ColumnSemantics:
    """LLM-inferred semantic information for one column."""

    table: str
    name: str
    role: str  # "dimension" | "measure" | "identifier" | "date" | "unknown"
    description: str
    suggested_measure_name: Optional[str] = (
        None  # populated only when role == "measure"
    )
    ontology_match: Optional[str] = (
        None  # populated when a curated ontology term matches the column
    )

    def to_json(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "suggested_measure_name": self.suggested_measure_name,
            "ontology_match": self.ontology_match,
        }


@dataclass
class MeasureSuggestion:
    """A measure the LLM thinks should exist on a table."""

    table: str
    name: str
    expression: str  # e.g. "SUM(Sales[Amount])"
    format_string: Optional[str] = None  # e.g. "$#,0.00"
    rationale: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "name": self.name,
            "expression": self.expression,
            "format_string": self.format_string,
            "rationale": self.rationale,
        }


@dataclass
class VisualSuggestion:
    """A visual the LLM thinks should exist on a page."""

    visual_type: str  # canonical TMDL visual type
    table: str
    measure: Optional[str] = None  # measure name; None for slicers
    dimension: Optional[str] = None  # column name
    rationale: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "visual_type": self.visual_type,
            "table": self.table,
            "measure": self.measure,
            "dimension": self.dimension,
            "rationale": self.rationale,
        }


@dataclass
class SchemaAdvisorResult:
    """Aggregate result of one advisor run."""

    column_semantics: List[ColumnSemantics] = field(default_factory=list)
    measure_suggestions: List[MeasureSuggestion] = field(default_factory=list)
    visual_suggestions: List[VisualSuggestion] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "column_semantics": [c.to_json() for c in self.column_semantics],
            "measure_suggestions": [m.to_json() for m in self.measure_suggestions],
            "visual_suggestions": [v.to_json() for v in self.visual_suggestions],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SchemaAdvisor:
    """LLM-driven schema enrichment for the planner payload.

    Wraps an LLM client that satisfies the orchestrator's
    ``LLMClient`` protocol — anything with a ``generate(messages)``
    method that returns a string. The advisor builds a structured
    prompt from the deterministic profile, parses the response,
    and returns a :class:`SchemaAdvisorResult`.

    Parameters
    ----------
    llm_client:
        Any object with a ``generate(messages)`` method.
    cache_size:
        Number of recent (prompt-fingerprint → result) entries to
        cache. Defaults to 32. Set to 0 to disable caching.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        cache_size: int = 32,
    ) -> None:
        self._llm = llm_client
        self._cache_size = cache_size
        self._cache: Dict[str, SchemaAdvisorResult] = {}

    def advise(self, profiles: Sequence[DataProfile]) -> Optional[SchemaAdvisorResult]:
        """Run the advisor over a deterministic profile set.

        Returns ``None`` when the LLM call fails or returns
        unparseable output, so the orchestrator can fall back to
        the deterministic profile unchanged. Also returns ``None``
        when ``profiles`` is empty — calling the LLM with no
        tables to analyse would be wasteful and the parsed result
        would be empty anyway.
        """
        if not profiles:
            return None
        prompt_fingerprint = self._fingerprint(profiles)
        if prompt_fingerprint in self._cache:
            return self._cache[prompt_fingerprint]

        prompt = self._build_prompt(profiles)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a senior data modeller helping to "
                    "design a Power BI semantic model. Given a "
                    "deterministic data profile, infer column "
                    "semantics, suggest measures, and recommend "
                    "visualisations. Respond ONLY with valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        try:
            raw = self._llm.generate(messages)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("SchemaAdvisor LLM call failed: %s", exc)
            return None

        try:
            parsed = self._parse_response(raw, profiles)
        except _AdvisorParseError as exc:
            _LOGGER.warning("SchemaAdvisor parse failed: %s", exc)
            return None

        if self._cache_size > 0:
            self._cache[prompt_fingerprint] = parsed
            # Drop oldest entries beyond the cap.
            if len(self._cache) > self._cache_size:
                first_key = next(iter(self._cache))
                self._cache.pop(first_key, None)
        return parsed

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------
    def _build_prompt(self, profiles: Sequence[DataProfile]) -> str:
        """Render the deterministic profile as a JSON prompt for the LLM.

        The LLM receives the same shape the planner payload will
        surface to it (``columns``, ``distinct_count``,
        ``distinct_examples``) but as a single compact object per
        table. We deliberately don't send row counts — cardinality
        of the distinct set is what drives measure and visual
        choices.
        """
        compact_tables = []
        for profile in profiles:
            for table in profile.tables:
                columns_brief = []
                for col in table.columns:
                    columns_brief.append(
                        {
                            "name": col.name,
                            "inferred_type": col.inferred_type,
                            "distinct_count": col.distinct_count,
                            "examples": col.distinct_examples[:5],
                        }
                    )
                compact_tables.append(
                    {
                        "name": table.name,
                        "columns": columns_brief,
                    }
                )
        payload = {
            "tables": compact_tables,
            "task": (
                "For each table, return: (1) column semantics with "
                "role (dimension / measure / identifier / date / "
                "unknown) and a short description, (2) measure "
                "suggestions for any numeric column that looks like "
                "an amount (name = 'Total <col>', expression = "
                "'SUM(<table>[<col>])'), (3) visual suggestions: "
                "for each (dimension, measure) pair, recommend "
                "the best visual type (barChart for categorical x "
                "measure, lineChart for date x measure, card for "
                "measure alone, pieChart for low-cardinality "
                "dimension x measure). Limit to 5 measure "
                "suggestions and 5 visual suggestions total. "
                "Respond with the exact JSON schema: "
                '{"columns": [{"table": str, "name": str, "role": '
                'str, "description": str, "suggested_measure_name": '
                'str|null}], "measures": [{"table": str, "name": '
                'str, "expression": str, "format_string": str|null, '
                '"rationale": str}], "visuals": [{"visual_type": '
                'str, "table": str, "measure": str|null, '
                '"dimension": str|null, "rationale": str}]}'
            ),
        }
        return json.dumps(payload, indent=2)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------
    def _parse_response(
        self, raw: str, profiles: Sequence[DataProfile]
    ) -> SchemaAdvisorResult:
        """Parse the LLM's JSON response into a typed result.

        Tolerant of code-fenced output (``\u0060\u0060\u0060json ...\u0060\u0060\u0060``),
        leading prose, and minor JSON malformations. Raises
        :class:`_AdvisorParseError` when the response is
        unrecoverable.
        """
        payload = self._extract_json(raw)
        if not isinstance(payload, dict):
            raise _AdvisorParseError(
                f"expected JSON object, got {type(payload).__name__}"
            )

        # Build a quick (table, name) → ColumnProfile map for validation.
        known_columns: Dict[Tuple[str, str], Any] = {}
        for profile in profiles:
            for table in profile.tables:
                for col in table.columns:
                    known_columns[(table.name, col.name)] = col

        result = SchemaAdvisorResult()

        for entry in payload.get("columns", []) or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            # Drop entries with no name — without a column name
            # the result is unusable and would clutter the
            # planner payload with empty-name garbage.
            if not name:
                continue
            table = str(entry.get("table", ""))
            role = str(entry.get("role", "unknown")).lower()
            if role not in {"dimension", "measure", "identifier", "date", "unknown"}:
                role = "unknown"
            description = str(entry.get("description", "")).strip()
            if not description:
                description = "No description provided."
            suggested_name = entry.get("suggested_measure_name")
            result.column_semantics.append(
                ColumnSemantics(
                    table=table,
                    name=name,
                    role=role,
                    description=description,
                    suggested_measure_name=(
                        str(suggested_name) if suggested_name else None
                    ),
                )
            )

        for entry in payload.get("measures", []) or []:
            if not isinstance(entry, dict):
                continue
            table = str(entry.get("table", ""))
            name = str(entry.get("name", ""))
            expression = str(entry.get("expression", ""))
            format_string = entry.get("format_string")
            rationale = str(entry.get("rationale", ""))
            if not (table and name and expression):
                continue
            result.measure_suggestions.append(
                MeasureSuggestion(
                    table=table,
                    name=name,
                    expression=expression,
                    format_string=str(format_string) if format_string else None,
                    rationale=rationale,
                )
            )

        for entry in payload.get("visuals", []) or []:
            if not isinstance(entry, dict):
                continue
            visual_type = str(entry.get("visual_type", ""))
            table = str(entry.get("table", ""))
            measure = entry.get("measure")
            dimension = entry.get("dimension")
            rationale = str(entry.get("rationale", ""))
            if not (visual_type and table):
                continue
            result.visual_suggestions.append(
                VisualSuggestion(
                    visual_type=visual_type,
                    table=table,
                    measure=str(measure) if measure else None,
                    dimension=str(dimension) if dimension else None,
                    rationale=rationale,
                )
            )

        return result

    @staticmethod
    def _extract_json(raw: str) -> Any:
        """Tolerate code fences and leading prose in the LLM response."""
        if not raw or not raw.strip():
            raise _AdvisorParseError("empty response")
        # Strip code fences (```json ... ``` or ``` ... ```).
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL)
        candidate = fenced.group(1) if fenced else raw
        # Try strict parse first.
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # Fall back: find the outermost { ... } block.
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise _AdvisorParseError("no JSON object found in response")
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise _AdvisorParseError(f"invalid JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------
    @staticmethod
    def _fingerprint(profiles: Sequence[DataProfile]) -> str:
        """Stable hash of the profile structure used as the cache key.

        Only structural fields count (table name, column name,
        type, distinct_count) — actual example values are excluded
        so the cache stays useful across rows with the same
        shape but different contents.
        """
        parts: List[str] = []
        for profile in profiles:
            for table in profile.tables:
                parts.append(table.name)
                for col in table.columns:
                    parts.append(col.name)
                    parts.append(col.inferred_type)
                    parts.append(str(col.distinct_count))
        raw = "|".join(parts).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:32]


class _AdvisorParseError(Exception):
    """Raised when the LLM's response can't be parsed as advisor JSON."""
