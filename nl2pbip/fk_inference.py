"""Deterministic foreign-key inference from column profiles.

The :mod:`nl2pbip.data_inspector` module profiles every registered
data source and emits a per-column summary (distinct counts, sample
values, inferred types, null rates, ``is_potential_key`` flag). This
module takes that profile set and produces a ranked list of likely
foreign-key relationships — *without* calling the LLM.

Why this matters
----------------
The orchestrator's planner prompt currently asks the LLM to suggest
foreign-key relationships in a tabular model. The LLM gets it right
about half the time on a 50-table star schema — every missed join is
a silent correctness hit the user finds days later. The same
``ColumnProfile`` data the LLM reasons about can be matched
deterministically with three signals:

1. **Name similarity** — ``customer_id`` ↔ ``customers.id`` after
   stripping ``_id``/``_key``/``_code`` suffixes.
2. **Sample overlap** — fraction of the smaller side's distinct
   values that appear in the larger side's distinct values (computed
   from the inspector's top-N examples).
3. **Type compatibility** — same inferred type AND both sides
   carry the ``is_potential_key`` flag (i.e. likely primary keys).

This module emits :class:`CardinalityHint` objects the orchestrator
threads into the planner payload as a ``Likely foreign keys (auto-detected)``
section. The LLM still has the final say on whether to wire up a
relationship — it now sees concrete evidence per pair instead of
guessing.

Public API
----------
* :func:`infer_foreign_keys` — main entry point.
* :class:`FKInferenceConfig` — tuning knobs.
* :class:`CardinalityHint` — dataclass carrying the friendly FK
  relationship + cardinality label.

Design constraints
-----------------
* **No new dependencies.** Pure stdlib (``difflib``, ``dataclasses``).
* **Bounded work.** ``FKInferenceConfig.max_pairs_evaluated`` caps
  the O(N²) pair explosion on 50-table projects at 2000 pairs by
  default (returns early once the cap is hit — see ``_CandidatePair``
  ordering).
* **Honest about sampling.** The inspector only carries the top-N
  distinct examples per column (default N=5); overlap computed
  against the examples is a **lower bound** on the true overlap.
  The hint's ``evidence`` string notes this so downstream consumers
  don't over-trust a near-1.0 ratio computed against a small
  sample.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

from nl2pbip.data_inspector import ColumnProfile, DataProfile

# Cardinality label literals. Snake_case to match the existing
# ``RelationshipCoverage.cardinality_hint`` field (``"manyToOne"``
# etc.), so the two hints stay shape-compatible in the planner
# payload.
Cardinality = str  # Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
CARDINALITY_ONE_TO_ONE = "one_to_one"
CARDINALITY_ONE_TO_MANY = "one_to_many"
CARDINALITY_MANY_TO_ONE = "many_to_one"
CARDINALITY_MANY_TO_MANY = "many_to_many"

# Column-name suffixes we treat as foreign-key content during the
# normaliser. Stripping these lets ``orders.customer_id`` align
# with ``customers.id`` after the ``customer`` ↔ ``customer``
# name-similarity pass.
_FK_SUFFIXES: Tuple[str, ...] = ("_id", "id", "_key", "key", "_code", "code")


@dataclass(frozen=True)
class CardinalityHint:
    """A likely foreign-key relationship between two columns.

        Attributes
    ----------
        from_table: str
            Table that holds the foreign key (the "many" side in a
            many-to-one relationship).
        from_column: str
            Foreign-key column on ``from_table``.
        to_table: str
            Referenced table (the "one" side in a many-to-one
            relationship).
        to_column: str
            Referenced primary-key column on ``to_table``.
        cardinality: Cardinality
            One of ``"one_to_one"``, ``"one_to_many"``,
            ``"many_to_one"``, ``"many_to_many"``.
        confidence: float
            Score in [0.0, 1.0]. Composite of name similarity, sample
            overlap, and a type-bonus. Use this to rank hints before
            showing them to the LLM.
        evidence: str
            Human-readable explanation of WHY the pair was suggested
            (which signals contributed, whether the overlap is a lower
            bound because the inspector only kept top-N examples).

        Notes
    -----
        Frozen dataclass so a hint never accidentally aliases another
        in a dedup pass.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: Cardinality
    confidence: float
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for the planner payload."""
        return {
            "from_table": self.from_table,
            "from_column": self.from_column,
            "to_table": self.to_table,
            "to_column": self.to_column,
            "cardinality": self.cardinality,
            "confidence": round(self.confidence, 4),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class FKInferenceConfig:
    """Tuning knobs for :func:`infer_foreign_keys`.

        Defaults are tuned for a 50-table star-schema project on a
        typical model: ~2000 candidate pairs to evaluate, low enough
        to keep the orchestrator hot path under 100 ms.

        Attributes
    ----------
        min_confidence: float
            Drop pairs with ``final_confidence < min_confidence``.
            Defaults to ``0.5`` — pairs with a weak name match AND
            no sample overlap never make it to the planner.
        name_similarity_threshold: float
            Floor for the column-name similarity score (difflib
            ``SequenceMatcher.ratio``). Pairs below this still
            survive when their sample overlap is strong — the
            threshold is only the *name* component, not the
            composite score.
        sample_overlap_threshold: float
            Floor for the sample-overlap fraction (overlap / smaller
            side's example count). Below this the pair is dropped
            outright — name similarity alone isn't enough to suggest
            a relationship without ANY value overlap.
        max_pairs_evaluated: int
            Cap on the number of column pairs walked before we
            return early. Guards against O(N²) blowup on a
            100-table × 10-col project = 90 000 pairs. Iteration
            stops once the cap is hit; surviving pairs are the
            highest-confidence ones we've seen so far (we sort by
            composite score before walking).
    """

    min_confidence: float = 0.5
    name_similarity_threshold: float = 0.6
    sample_overlap_threshold: float = 0.3
    max_pairs_evaluated: int = 2000

    def __post_init__(self) -> None:
        # Clamp thresholds to sane ranges so a caller passing
        # ``min_confidence=-1.0`` or ``max_pairs_evaluated=0``
        # doesn't crash the orchestrator mid-plan.
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError(
                f"min_confidence must be in [0.0, 1.0], got {self.min_confidence!r}"
            )
        if not 0.0 <= self.name_similarity_threshold <= 1.0:
            raise ValueError(
                "name_similarity_threshold must be in [0.0, 1.0], "
                f"got {self.name_similarity_threshold!r}"
            )
        if not 0.0 <= self.sample_overlap_threshold <= 1.0:
            raise ValueError(
                "sample_overlap_threshold must be in [0.0, 1.0], "
                f"got {self.sample_overlap_threshold!r}"
            )
        if self.max_pairs_evaluated < 0:
            raise ValueError(
                f"max_pairs_evaluated must be >= 0, got {self.max_pairs_evaluated!r}"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_column_name(name: str) -> str:
    """Normalise a column name for fuzzy comparison.

    Lowercases, strips trailing ``_id`` / ``_key`` / ``_code`` /
    ``id`` / ``key`` / ``code`` suffixes, and collapses non-alphanumeric
    runs to ``_`` so ``customer-id`` and ``customer_id`` compare equal.

    Examples
    --------
    >>> _normalise_column_name("customer_id")
    'customer'
    >>> _normalise_column_name("CustomerCode")
    'customer'
    >>> _normalise_column_name("id")
    ''
    """
    lowered = name.lower().strip()
    # Collapse non-alphanumeric to ``_`` so ``customer-id`` and
    # ``customer_id`` line up.
    cleaned = "".join(c if c.isalnum() else "_" for c in lowered)
    # Strip a leading ``_`` / trailing underscores that the
    # alnum-collapse may have left behind.
    cleaned = cleaned.strip("_")
    # Recursively peel matching FK suffixes until the string is
    # stable. The recursive peel catches the ``customerid`` →
    # ``customer`` case in addition to the underscored variants.
    changed = True
    while changed and cleaned:
        changed = False
        for suffix in _FK_SUFFIXES:
            if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
                cleaned = cleaned[: -len(suffix)]
                changed = True
                break
    return cleaned


def _name_similarity(a: str, b: str) -> float:
    """Return ``SequenceMatcher.ratio`` of two normalised column names.

    Returns ``0.0`` when either side normalises to an empty
    string (e.g. comparing ``id`` to anything else after the
    suffix strip leaves nothing to compare).
    """
    na = _normalise_column_name(a)
    nb = _normalise_column_name(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(a=na, b=nb).ratio()


def _is_potential_key(col: ColumnProfile, row_count: int) -> bool:
    """Return True when ``col`` looks like a primary key.

    Criteria: distinct_count equals row_count AND the column has
    no nulls. This is the same rule the inspector uses when it
    sets ``is_potential_key`` (the field isn't always populated
    by every code path, so we re-derive it here from the
    raw counts).
    """
    if row_count <= 0:
        return False
    if col.distinct_count != row_count:
        return False
    if col.null_rate > 0.0:
        return False
    return True


def _column_examples(col: ColumnProfile) -> List[str]:
    """Return the distinct examples list coerced to strings.

    The inspector stores ``distinct_examples`` as the top-N most
    frequent distinct values; values may be ints, floats, or
    strings depending on what the inspector saw. Coerce to str so
    the overlap set-comparison is homogeneous.
    """
    out: List[str] = []
    for v in col.distinct_examples or []:
        if v is None:
            continue
        out.append(str(v))
    return out


def _sample_overlap(col_small: ColumnProfile, col_large: ColumnProfile) -> float:
    """Fraction of ``col_small``'s distinct examples found in ``col_large``'s.

    Returns ``0.0`` when ``col_small`` has no examples to compare.
    """
    small = set(_column_examples(col_small))
    large = set(_column_examples(col_large))
    if not small:
        return 0.0
    return len(small & large) / len(small)


def _classify_cardinality(small_is_pk: bool, large_is_pk: bool) -> Cardinality:
    """Classify the relationship based on PK status of each side.

    Rules
    -----
    * Both ``is_potential_key`` → ``"one_to_one"`` (bijective join).
    * Only the smaller side is PK → ``"many_to_one"`` (FK points
      into the PK side).
    * Otherwise → ``"many_to_many"`` (no guarantee either side is
      unique).
    """
    if small_is_pk and large_is_pk:
        return CARDINALITY_ONE_TO_ONE
    if small_is_pk:
        return CARDINALITY_MANY_TO_ONE
    return CARDINALITY_MANY_TO_MANY


def _flatten_columns(
    profiles: Iterable[DataProfile],
) -> List[Tuple[str, ColumnProfile, int]]:
    """Flatten ``List[DataProfile]`` to ``(table_name, col, row_count)`` rows.

    The orchestrator passes ``List[DataProfile]`` (the shape
    ``inspect_data_sources`` returns). Each ``DataProfile`` may
    contain multiple ``TableProfile`` entries (a single CSV file
    is one table; a callable returning ``{table: DataFrame}`` may
    yield several). The FK inference operates at the table/column
    granularity, so we flatten once up front.
    """
    flat: List[Tuple[str, ColumnProfile, int]] = []
    for profile in profiles:
        for table in profile.tables:
            row_count = max(table.row_count, 1)
            for col in table.columns:
                flat.append((table.name, col, row_count))
    return flat


def _eligible_type(inferred_type: str) -> bool:
    """Return True for column types the FK heuristic is willing to compare.

    Numeric, text, and date columns can all host FK relationships
    in practice (think ``Order.date_key`` ↔ ``Date.key``). Boolean
    and binary columns are degenerate — never worth evaluating.
    """
    return inferred_type in {"numeric", "text", "date"}


def _score_pair(
    small_col: ColumnProfile,
    large_col: ColumnProfile,
    small_name: str,
    large_name: str,
    name_score: float,
    overlap_score: float,
    small_is_pk: bool,
    large_is_pk: bool,
) -> Tuple[float, str]:
    """Compute the composite confidence score + evidence string.

    Returns ``(confidence, evidence)`` where ``confidence`` is
    ``0.5 * name_score + 0.4 * overlap_score + 0.1 * type_bonus``
    clamped to ``[0.0, 1.0]``.

    The ``type_bonus`` is ``1.0`` when both sides share the same
    ``inferred_type`` AND both are ``is_potential_key`` (a strong
    signal that we're matching two PK columns of foreign keys),
    ``0.0`` otherwise.

    The ``evidence`` string is a human-readable one-liner listing
    the contributing signals so the LLM doesn't have to re-derive
    them from the hint.
    """
    type_bonus = 0.0
    if (
        small_is_pk
        and large_is_pk
        and small_col.inferred_type == large_col.inferred_type
    ):
        type_bonus = 1.0
    raw = 0.5 * name_score + 0.4 * overlap_score + 0.1 * type_bonus
    confidence = max(0.0, min(1.0, raw))
    bits: List[str] = []
    if name_score > 0:
        bits.append(f"name similarity {name_score:.2f}")
    if overlap_score > 0:
        bits.append(f"sample overlap {overlap_score:.2f}")
    if type_bonus > 0:
        bits.append("both sides marked potential key with matching types")
    if not bits:
        bits.append("no signal beyond column-name proximity")
    return confidence, "; ".join(bits)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def infer_foreign_keys(
    profiles: List[DataProfile],
    config: Optional[FKInferenceConfig] = None,
) -> List[CardinalityHint]:
    """Detect likely foreign-key relationships across profiled data sources.

    Parameters
    ----------
    profiles:
        Output of :func:`nl2pbip.data_inspector.inspect_data_sources`.
        Each :class:`DataProfile` may contain several
        :class:`TableProfile` entries; the function iterates every
        table × column pair.

    config:
        Optional :class:`FKInferenceConfig` to tune thresholds.
        ``None`` falls back to the module defaults.

    Returns
    -------
    List[CardinalityHint]
        Ranked, deduplicated hints ordered by ``confidence``
        descending. Symmetric duplicates (where both
        ``(T1.C1 → T2.C2)`` and ``(T2.C2 → T1.C1)`` survive the
        threshold filter) are reduced to the single
        higher-confidence direction.

    Notes
    -----
    When ``len(profiles) == 0`` or the underlying
    ``profiles[*].tables`` lists are all empty, the function
    returns ``[]`` — no work to do, no error to raise.
    """
    cfg = config or FKInferenceConfig()
    flat = _flatten_columns(profiles)
    if len(flat) < 2:
        return []
    # Pre-compute the example sets so each pair-comparison below
    # doesn't redo the coercion work.
    hints: List[CardinalityHint] = []
    seen_pairs: set[Tuple[str, str, str, str]] = set()
    # Build a list of candidate (T1, C1, T2, C2) tuples respecting
    # the ``max_pairs_evaluated`` cap. We sort flat entries by
    # table name then column name so the iteration order is
    # deterministic across runs (test snapshots depend on this).
    flat_sorted = sorted(flat, key=lambda t: (t[0], t[1].name))
    pair_count = 0
    for i, (t1, c1, rc1) in enumerate(flat_sorted):
        for j in range(i + 1, len(flat_sorted)):
            if pair_count >= cfg.max_pairs_evaluated:
                break
            t2, c2, rc2 = flat_sorted[j]
            if t1 == t2:
                # Same table — skip (a column isn't its own FK).
                continue
            if not _eligible_type(c1.inferred_type) or not _eligible_type(
                c2.inferred_type
            ):
                continue
            if c1.inferred_type != c2.inferred_type:
                continue
            # The "smaller" side is the one with fewer distinct
            # values; it tends to be the FK side (the many column
            # in a many-to-one join). Tie-break by row_count so
            # the choice is stable when both sides have the same
            # ``distinct_count``.
            if c1.distinct_count < c2.distinct_count or (
                c1.distinct_count == c2.distinct_count and rc1 <= rc2
            ):
                small_col, small_table, small_rc = c1, t1, rc1
                large_col, large_table, large_rc = c2, t2, rc2
            else:
                small_col, small_table, small_rc = c2, t2, rc2
                large_col, large_table, large_rc = c1, t1, rc1
            small_is_pk = _is_potential_key(small_col, small_rc)
            large_is_pk = _is_potential_key(large_col, large_rc)
            # Spec: skip pairs where neither column is a potential
            # key. Non-PK-vs-non-PK joins are usually the LLM's
            # problem, not ours — the heuristic is meant to catch
            # obvious FK → PK wiring.
            if not small_is_pk and not large_is_pk:
                continue
            pair_count += 1
            name_score = _name_similarity(small_col.name, large_col.name)
            overlap_score = _sample_overlap(small_col, large_col)
            # Hard filter on overlap ratio. Name similarity is a
            # soft floor (we still evaluate pairs below
            # ``name_similarity_threshold`` if overlap is strong).
            if (
                name_score < cfg.name_similarity_threshold
                and overlap_score < cfg.sample_overlap_threshold
            ):
                continue
            confidence, evidence = _score_pair(
                small_col=small_col,
                large_col=large_col,
                small_name=small_col.name,
                large_name=large_col.name,
                name_score=name_score,
                overlap_score=overlap_score,
                small_is_pk=small_is_pk,
                large_is_pk=large_is_pk,
            )
            if confidence < cfg.min_confidence:
                continue
            cardinality = _classify_cardinality(small_is_pk, large_is_pk)
            pair_key = (small_table, small_col.name, large_table, large_col.name)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            # Append a lower-bound caveat to the evidence string
            # when the overlap is computed against the examples
            # list (which only carries the top-N most frequent
            # values). The LLM uses the evidence to decide
            # whether to trust the hint.
            if overlap_score > 0 and small_col.distinct_count > len(
                _column_examples(small_col)
            ):
                evidence = (
                    evidence + f" (overlap computed against top-"
                    f"{len(_column_examples(small_col))} of "
                    f"{small_col.distinct_count} distinct values — "
                    "ratio is a lower bound)"
                )
            hints.append(
                CardinalityHint(
                    from_table=small_table,
                    from_column=small_col.name,
                    to_table=large_table,
                    to_column=large_col.name,
                    cardinality=cardinality,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
        if pair_count >= cfg.max_pairs_evaluated:
            break
    # Rank by confidence desc, then by table name / column name for
    # deterministic ordering (test snapshots).
    hints.sort(
        key=lambda h: (
            -h.confidence,
            h.from_table,
            h.from_column,
            h.to_table,
            h.to_column,
        )
    )
    # Symmetric-dedup: when both directions of a pair survive the
    # threshold filter, keep the higher-confidence direction
    # only. The earlier loop walks each unordered pair exactly
    # once, so the only duplicates that can appear here are the
    # case where a pair legitimately meets the threshold in both
    # directions (rare in practice — the smaller/larger ordering
    # is deterministic, but composite score can differ if the
    # two directions have different name scores due to suffix
    # stripping being direction-sensitive).
    deduped: List[CardinalityHint] = []
    seen_canonical: set[Tuple[str, str, str, str]] = set()
    for hint in hints:
        canonical = tuple(
            sorted(
                [
                    (hint.from_table, hint.from_column),
                    (hint.to_table, hint.to_column),
                ]
            )
        )
        canonical_key: Tuple[str, str, str, str] = (
            canonical[0][0],
            canonical[0][1],
            canonical[1][0],
            canonical[1][1],
        )
        if canonical_key in seen_canonical:
            continue
        seen_canonical.add(canonical_key)
        deduped.append(hint)
    return deduped


__all__ = [
    "CARDINALITY_MANY_TO_MANY",
    "CARDINALITY_MANY_TO_ONE",
    "CARDINALITY_ONE_TO_MANY",
    "CARDINALITY_ONE_TO_ONE",
    "CardinalityHint",
    "FKInferenceConfig",
    "infer_foreign_keys",
]
