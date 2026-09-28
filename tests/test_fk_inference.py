"""Unit tests for the FK-inference module.

Covers the deterministic foreign-key detection heuristics in
:mod:`nl2pbip.fk_inference`. Every test builds a minimal
``DataProfile`` / ``TableProfile`` / ``ColumnProfile`` triple by
hand so the assertions are independent of the inspector's own
sampling logic.

Test matrix
-----------

* **Name-match** — exact-match column names push confidence near 1.0.
* **Table-prefix normalisation** — ``customer_id`` ↔ ``id`` still
  matches via the ``_id`` suffix strip.
* **Sample overlap** — overlap drives confidence above the
  min-confidence floor; zero overlap pulls it below.
* **Cardinality classification** — both sides ``is_potential_key``
  → ``oneToOne``; exactly one side PK → ``manyToOne`` (the
  non-PK side is "many" because it has duplicates).
* **Threshold filtering** — low-confidence pairs are dropped when
  ``min_confidence`` is raised.
* **Symmetric dedup** — both directions of a pair collapse to one.
* **``max_pairs_evaluated`` cap** — early termination on huge inputs.
* **Type filter** — incompatible inferred types never produce hints.
* **Non-PK filter** — pairs where neither side is PK never produce hints.
* **Orchestrator integration** — the planner payload includes the
  FK section when data sources are registered, and the section is
  omitted when ``fk_inference_enabled=False`` (or when the CLI's
  ``--disable-fk-inference`` flag is set).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.data_inspector import ColumnProfile, DataProfile, TableProfile
from nl2pbip.fk_inference import (
    CARDINALITY_MANY_TO_MANY,
    CARDINALITY_MANY_TO_ONE,
    CARDINALITY_ONE_TO_ONE,
    CardinalityHint,
    FKInferenceConfig,
    _flatten_columns,
    _name_similarity,
    _normalise_column_name,
    _sample_overlap,
    infer_foreign_keys,
)
from nl2pbip.orchestrator import Orchestrator
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _col(
    name: str,
    *,
    inferred_type: str = "text",
    distinct_count: int | None = None,
    row_count: int = 5,
    null_rate: float = 0.0,
    examples: List[str] | None = None,
) -> ColumnProfile:
    """Build a ``ColumnProfile`` for tests.

    When ``distinct_count`` is omitted it defaults to
    ``len(examples)`` (so the PK check in
    :func:`_is_potential_key` agrees with the examples list).
    Pass ``distinct_count`` explicitly to force a different
    ratio.
    """
    examples_list = list(examples or [])
    resolved_distinct = (
        distinct_count if distinct_count is not None else max(len(examples_list), 1)
    )
    return ColumnProfile(
        name=name,
        inferred_type=inferred_type,
        non_null_count=row_count,
        distinct_count=resolved_distinct,
        null_rate=null_rate,
        distinct_examples=examples_list,
    )


def _table(
    name: str,
    columns: List[ColumnProfile],
    *,
    row_count: int = 5,
) -> TableProfile:
    return TableProfile(
        name=name,
        row_count=row_count,
        sampled_at_least=row_count,
        columns=columns,
    )


def _profile(
    source_name: str,
    tables: List[TableProfile],
) -> DataProfile:
    return DataProfile(
        source_name=source_name,
        source_kind="records",
        tables=tables,
    )


# ---------------------------------------------------------------------------
# Helpers / pure-function tests
# ---------------------------------------------------------------------------


class TestNormaliseColumnName:
    def test_strips_underscore_id_suffix(self) -> None:
        assert _normalise_column_name("customer_id") == "customer"

    def test_strips_bare_id_suffix(self) -> None:
        # Both underscored and bare suffixes normalise to the same thing.
        assert _normalise_column_name("customerid") == "customer"

    def test_lowercases_and_underscores(self) -> None:
        # ``CustomerCode`` and ``customer_code`` compare equal.
        assert _normalise_column_name("CustomerCode") == _normalise_column_name(
            "customer_code"
        )

    def test_bare_id_returns_empty(self) -> None:
        # ``id`` strips to nothing — callers should fall back to
        # the table-prefix match (we don't do that here, but the
        # empty string is a useful sentinel).
        assert _normalise_column_name("id") == ""


class TestNameSimilarity:
    def test_exact_match(self) -> None:
        assert _name_similarity("customer_id", "customer_id") == 1.0

    def test_suffix_strip_boosts_match(self) -> None:
        # ``orders.customer_id`` ↔ ``customers.id`` — after the
        # suffix strip, ``customer`` ↔ ``customer`` is a near-perfect
        # match. Without the strip the ratio is much lower.
        assert _name_similarity("customer_id", "id") > 0.6

    def test_completely_unrelated_returns_low(self) -> None:
        score = _name_similarity("region", "amount")
        # SequenceMatcher still finds some overlap on the
        # common-letter structure (e.g. ``r`` appears once in
        # each); a 0..1 lower bound is enough.
        assert score < 0.5


class TestIsPotentialKey:
    def test_unique_no_nulls(self) -> None:
        col = _col("id", distinct_count=5, row_count=5)
        assert col.is_potential_key(row_count=5) is True

    def test_duplicates_not_pk(self) -> None:
        col = _col("id", distinct_count=4, row_count=5)
        assert col.is_potential_key(row_count=5) is False

    def test_nulls_exclude_pk(self) -> None:
        col = _col("id", distinct_count=5, row_count=5, null_rate=0.1)
        assert col.is_potential_key(row_count=5) is False


class TestSampleOverlap:
    def test_full_overlap(self) -> None:
        small = _col("a", examples=["1", "2", "3"])
        large = _col("b", examples=["1", "2", "3", "4"])
        assert _sample_overlap(small, large) == 1.0

    def test_partial_overlap(self) -> None:
        small = _col("a", examples=["1", "2", "3", "4"])
        large = _col("b", examples=["1", "2"])
        assert _sample_overlap(small, large) == 0.5

    def test_no_overlap(self) -> None:
        small = _col("a", examples=["1", "2"])
        large = _col("b", examples=["3", "4"])
        assert _sample_overlap(small, large) == 0.0

    def test_empty_examples_returns_zero(self) -> None:
        small = _col("a", examples=[])
        large = _col("b", examples=["1"])
        assert _sample_overlap(small, large) == 0.0


class TestFlattenColumns:
    def test_flattens_nested_profiles(self) -> None:
        profile = _profile(
            "src",
            [
                _table("orders", [_col("id"), _col("amount")]),
                _table("customers", [_col("id")]),
            ],
        )
        flat = _flatten_columns([profile])
        # Three columns across two tables.
        assert len(flat) == 3
        # Each entry is (table_name, ColumnProfile, row_count).
        tables = sorted({t for t, _, _ in flat})
        names = sorted(c.name for _, c, _ in flat)
        assert tables == ["customers", "orders"]
        assert names == ["amount", "id", "id"]


# ---------------------------------------------------------------------------
# infer_foreign_keys: scoring tests
# ---------------------------------------------------------------------------


class TestInferForeignKeys:
    def test_name_match_exact(self) -> None:
        """Two columns both named ``customer_id`` → confidence >= 0.9."""
        profile = _profile(
            "src",
            [
                _table(
                    "orders",
                    [_col("customer_id", examples=["c1", "c2", "c3", "c4", "c5"])],
                ),
                _table(
                    "customers",
                    [_col("customer_id", examples=["c1", "c2", "c3", "c4", "c5"])],
                ),
            ],
        )
        hints = infer_foreign_keys([profile])
        assert hints
        # Both columns have all 5 distinct values + same examples →
        # strong name match + full overlap + PK/PK type bonus.
        assert hints[0].confidence >= 0.9
        assert hints[0].from_column == "customer_id"
        assert hints[0].to_column == "customer_id"

    def test_name_match_with_table_prefix(self) -> None:
        """``orders.customer_id`` vs ``customers.id`` → confidence >= 0.5."""
        profile = _profile(
            "src",
            [
                _table(
                    "orders",
                    [_col("customer_id", examples=["c1", "c2", "c3", "c4", "c5"])],
                ),
                _table(
                    "customers",
                    [_col("id", examples=["c1", "c2", "c3", "c4", "c5"])],
                ),
            ],
        )
        hints = infer_foreign_keys([profile])
        assert hints
        # Suffix strip normalises both to ``customer``, so name score is 1.0.
        # Plus full sample overlap → confidence well above the 0.5 floor.
        assert hints[0].confidence >= 0.5
        # The smaller-cardinality side is ``customers.id`` (5 distinct,
        # 5 rows) tied with ``orders.customer_id``; the tie-break picks
        # ``customers.id`` as the FK side (smaller row_count ties lose
        # in the equal-distinct-count branch — either direction is OK,
        # just assert the pair is connected).
        from_pairs = {(h.from_table, h.to_table) for h in hints}
        assert from_pairs == {("customers", "orders")} or from_pairs == {
            ("orders", "customers")
        }

    def test_value_overlap_boosts_confidence(self) -> None:
        """Same column names + no value overlap → confidence < 0.5; with overlap → >= 0.7."""
        # No-overlap version
        no_overlap_profile = _profile(
            "src",
            [
                _table("a", [_col("customer_id", examples=["x1", "x2", "x3"])]),
                _table("b", [_col("customer_id", examples=["y1", "y2", "y3"])]),
            ],
        )
        no_overlap_hints = infer_foreign_keys([no_overlap_profile])
        if no_overlap_hints:
            assert no_overlap_hints[0].confidence < 0.5

        # With overlap (same examples)
        overlap_profile = _profile(
            "src",
            [
                _table(
                    "a",
                    [_col("customer_id", examples=["c1", "c2", "c3", "c4", "c5"])],
                ),
                _table(
                    "b",
                    [_col("customer_id", examples=["c1", "c2", "c3", "c4", "c5"])],
                ),
            ],
        )
        overlap_hints = infer_foreign_keys([overlap_profile])
        assert overlap_hints
        assert overlap_hints[0].confidence >= 0.7

    def test_cardinality_one_to_one(self) -> None:
        """Both sides ``is_potential_key`` → emits ``one_to_one``."""
        # 5 rows, 5 distinct values on both sides, no nulls → both PK.
        profile = _profile(
            "src",
            [
                _table(
                    "a",
                    [
                        _col(
                            "user_id",
                            distinct_count=5,
                            row_count=5,
                            examples=["u1", "u2", "u3", "u4", "u5"],
                        )
                    ],
                ),
                _table(
                    "b",
                    [
                        _col(
                            "user_id",
                            distinct_count=5,
                            row_count=5,
                            examples=["u1", "u2", "u3", "u4", "u5"],
                        )
                    ],
                ),
            ],
        )
        hints = infer_foreign_keys([profile])
        assert hints
        assert hints[0].cardinality == CARDINALITY_ONE_TO_ONE

    def test_cardinality_many_to_one(self) -> None:
        """Only smaller side is potential_key → emits ``many_to_one``."""
        # ``orders.customer_id`` is NOT PK (3 distinct / 5 rows),
        # ``customers.id`` is PK (5 distinct / 5 rows).
        profile = _profile(
            "src",
            [
                _table(
                    "orders",
                    [
                        _col(
                            "customer_id",
                            distinct_count=3,
                            row_count=5,
                            examples=["c1", "c2", "c3"],
                        )
                    ],
                ),
                _table(
                    "customers",
                    [
                        _col(
                            "id",
                            distinct_count=5,
                            row_count=5,
                            examples=["c1", "c2", "c3", "c4", "c5"],
                        )
                    ],
                ),
            ],
        )
        hints = infer_foreign_keys([profile])
        assert hints
        assert hints[0].cardinality == CARDINALITY_MANY_TO_ONE
        # The FK side (``from_table``) is the smaller-distinct side.
        # ``orders.customer_id`` has 3 distinct / 5 rows; ``customers.id``
        # has 5 distinct / 5 rows → from_table = "orders".
        assert hints[0].from_table == "orders"
        assert hints[0].to_table == "customers"

    def test_cardinality_small_pk_only(self) -> None:
        """Only smaller-distinct side PK → emits ``manyToOne`` (R-N-01).

        The old helper labelled this case ``many_to_many`` (R-N-01)
        because the larger side had duplicates but its distinct
        count was higher — a degenerate setup. The corrected
        helper (R-N-01) collapses both "exactly one side is PK"
        cases to ``manyToOne``: the non-PK side is "many" because
        it has duplicates, the PK side is "one" regardless of
        which physical column carries fewer distinct values.

        The ``from_table`` of the emitted hint is still the
        smaller-distinct side (``customers``), matching the
        pre-existing ``test_cardinality_many_to_one`` pattern —
        callers use the dedup key (sorted ``from``/``to``)
        rather than ``from`` alone for relationship wiring.
        """
        # ``customers.tag`` is the smaller-distinct side AND PK
        # (5 distinct / 5 rows). ``orders.tag`` is larger (10
        # distinct) but NOT PK (15 rows > 10 distinct — has
        # duplicates, so the FK side).
        profile = _profile(
            "src",
            [
                _table(
                    "orders",
                    [
                        _col(
                            "tag",
                            distinct_count=10,
                            row_count=15,
                            examples=["t1", "t2", "t3", "t4", "t5"],
                        )
                    ],
                ),
                _table(
                    "customers",
                    [
                        _col(
                            "tag",
                            distinct_count=5,
                            row_count=5,
                            examples=["t1", "t2", "t3", "t4", "t5"],
                        )
                    ],
                ),
            ],
        )
        hints = infer_foreign_keys([profile])
        assert hints
        # Smaller-distinct side (customers, 5 distinct) is PK;
        # larger side (orders, 10 distinct / 15 rows) has
        # duplicates → FK. Cardinality is manyToOne.
        assert hints[0].cardinality == CARDINALITY_MANY_TO_ONE
        assert hints[0].from_table == "customers"
        assert hints[0].to_table == "orders"

    def test_drops_below_min_confidence(self) -> None:
        """``min_confidence=0.9`` filters out medium-confidence pairs."""
        # Medium-quality pair: name similarity ~1.0 (suffix strip
        # makes ``customer_id`` ↔ ``id`` match), but only partial
        # sample overlap (3 of 5 values match).
        profile = _profile(
            "src",
            [
                _table(
                    "a",
                    [_col("customer_id", examples=["c1", "c2", "c3", "x4", "x5"])],
                ),
                _table(
                    "b",
                    [_col("id", examples=["c1", "c2", "c3", "y4", "y5"])],
                ),
            ],
        )
        # Default config keeps the pair (it's above 0.5).
        default_hints = infer_foreign_keys([profile])
        assert default_hints
        # Aggressive config drops it.
        aggressive_hints = infer_foreign_keys(
            [profile], config=FKInferenceConfig(min_confidence=0.9)
        )
        assert aggressive_hints == []

    def test_drops_symmetric_duplicate(self) -> None:
        """Both (T1.C1 → T2.C2) and reverse collapse to one hint."""
        # Set up so that both directions of the pair would meet
        # the threshold independently — i.e. the name score,
        # overlap score, and cardinality classification are the
        # same whichever side is the "small" one. This happens
        # when both sides have identical distinct_count and
        # row_count and matching examples.
        profile = _profile(
            "src",
            [
                _table(
                    "a",
                    [
                        _col(
                            "customer_id",
                            distinct_count=5,
                            row_count=5,
                            examples=["c1", "c2", "c3", "c4", "c5"],
                        )
                    ],
                ),
                _table(
                    "b",
                    [
                        _col(
                            "customer_id",
                            distinct_count=5,
                            row_count=5,
                            examples=["c1", "c2", "c3", "c4", "c5"],
                        )
                    ],
                ),
            ],
        )
        hints = infer_foreign_keys([profile])
        # At most one hint for the (a, b) ↔ (b, a) pair.
        assert len(hints) == 1

    def test_max_pairs_evaluated_caps_work(self) -> None:
        """100 tables × 10 cols = 90 000 pairs; cap at 50 returns early.

        R-N-22: previously the test only asserted
        ``isinstance(hints, list)`` — a silent soft-pass. Now we
        assert that the cap is honoured: the function terminates
        before exhausting the 19 900 candidate pairs, and any
        hints returned respect the cap (we don't assert a specific
        count, just that it's bounded by what the function could
        produce in 50 evaluations worth of work).
        """
        tables: List[TableProfile] = []
        for i in range(20):
            tables.append(
                _table(
                    f"t{i}",
                    [
                        _col(
                            f"c{j}",
                            distinct_count=5,
                            row_count=5,
                            examples=[f"v{j}_{k}" for k in range(5)],
                        )
                        for j in range(10)
                    ],
                )
            )
        profile = _profile("src", tables)
        # 20 tables × 10 cols = 200 cols. Pairs of (i, j) where i < j:
        # 200 * 199 / 2 = 19 900. Cap at 50 → early termination.
        # Without the cap, the full 19 900-pair walk would
        # dominate CI time on slow runners.
        hints_default = infer_foreign_keys([profile])
        hints_capped = infer_foreign_keys(
            [profile], config=FKInferenceConfig(max_pairs_evaluated=50)
        )
        # The capped walk must not hang — pytest will catch that
        # via its own watchdog. The bounded assert is that the
        # capped run returns a strict subset (or equal) of the
        # uncapped run's hints, since the cap can only drop work.
        assert len(hints_capped) <= len(hints_default)
        # And the cap must trigger (otherwise the test isn't
        # exercising the early-termination branch). Compute the
        # expected number of pairs evaluated: with flat_sorted of
        # length 200 the i<j outer loop has 200*199/2 = 19 900
        # iterations, but the inner pair_count increment only
        # fires when the pair survives the type/PK filter — so we
        # don't assert a strict upper bound, just that the cap
        # bound on hints returned is plausible. Use the
        # ``min_confidence`` floor: with a high floor the
        # uncapped walk should still return many hints and the
        # capped walk should clearly cut them down.
        fierce_hints_default = infer_foreign_keys(
            [profile], config=FKInferenceConfig(min_confidence=0.0)
        )
        fierce_hints_capped = infer_foreign_keys(
            [profile],
            config=FKInferenceConfig(max_pairs_evaluated=50, min_confidence=0.0),
        )
        assert len(fierce_hints_capped) < len(fierce_hints_default)

    def test_skips_non_matching_types(self) -> None:
        """string col vs int col never emits a hint (R-N-23)."""
        profile = _profile(
            "src",
            [
                _table("a", [_col("id", inferred_type="text", examples=["x1", "x2"])]),
                _table("b", [_col("id", inferred_type="numeric", examples=["1", "2"])]),
            ],
        )
        hints = infer_foreign_keys([profile])
        # R-N-23: was a soft-pass that asserted "this hint isn't
        # in the output". Now assert that the pair is fully
        # absent: no hint should reference both (a, b).
        matching = [
            h
            for h in hints
            if h.from_column == "id" and {h.from_table, h.to_table} == {"a", "b"}
        ]
        assert (
            matching == []
        ), f"Expected no hint for text-vs-numeric pair, got {matching}"

    def test_skips_when_neither_potential_key(self) -> None:
        """Two ``is_potential_key=False`` columns never emit a hint."""
        # 5 rows but only 2 distinct values on each side → neither PK.
        profile = _profile(
            "src",
            [
                _table(
                    "a",
                    [_col("tag", distinct_count=2, row_count=5, examples=["x", "y"])],
                ),
                _table(
                    "b",
                    [_col("tag", distinct_count=2, row_count=5, examples=["x", "y"])],
                ),
            ],
        )
        hints = infer_foreign_keys([profile])
        # The heuristic skips pairs where neither side is PK, so no
        # hint is emitted even though the names match and the values
        # overlap perfectly.
        assert hints == []


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestFKInferenceConfig:
    def test_rejects_negative_min_confidence(self) -> None:
        with pytest.raises(ValueError, match="min_confidence"):
            FKInferenceConfig(min_confidence=-0.1)

    def test_rejects_oversized_min_confidence(self) -> None:
        with pytest.raises(ValueError, match="min_confidence"):
            FKInferenceConfig(min_confidence=1.5)

    def test_rejects_negative_max_pairs(self) -> None:
        with pytest.raises(ValueError, match="max_pairs_evaluated"):
            FKInferenceConfig(max_pairs_evaluated=-1)


# ---------------------------------------------------------------------------
# Empty-input + dataclass-shape smoke tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_profiles_returns_empty_list(self) -> None:
        assert infer_foreign_keys([]) == []

    def test_profile_with_no_tables_returns_empty(self) -> None:
        profile = _profile("empty", [])
        assert infer_foreign_keys([profile]) == []

    def test_cardinality_hint_to_dict_round_trips(self) -> None:
        hint = CardinalityHint(
            from_table="orders",
            from_column="customer_id",
            to_table="customers",
            to_column="id",
            cardinality=CARDINALITY_MANY_TO_ONE,
            confidence=0.85,
            evidence="name match + sample overlap",
        )
        d = hint.to_dict()
        assert d["from_table"] == "orders"
        assert d["from_column"] == "customer_id"
        assert d["to_table"] == "customers"
        assert d["to_column"] == "id"
        assert d["cardinality"] == CARDINALITY_MANY_TO_ONE
        assert d["confidence"] == 0.85
        assert d["evidence"]


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class _StaticFKLLM:
    """LLM stub that captures the planner payload it was sent.

    Records every ``messages`` list it receives on ``self.captured``
    so the integration test can assert what the orchestrator
    surfaced to the LLM (rather than asserting on the full
    ``_planner_payload`` dict).
    """

    provider = "stub"
    model = "stub-fk-model"

    def __init__(self) -> None:
        self.captured: List[List[Dict[str, Any]]] = []

    def generate(self, messages: List[Dict[str, str]]) -> str:  # type: ignore[override]
        self.captured.append(list(messages))
        return json.dumps({"plan": []})


class TestOrchestratorIntegration:
    def _make_ctx(
        self,
        tmp_path: Path,
        sources: Dict[str, Any],
        fk_enabled: bool = True,
    ) -> Dict[str, Any]:
        return {
            MODEL_PATH_KEY: str(tmp_path / "model.tmdl"),
            REPORT_PATH_KEY: str(tmp_path / "report.json"),
            "data_sources": sources,
            "fk_inference_enabled": fk_enabled,
        }

    def test_orchestrator_includes_fk_section_in_plan_prompt(
        self, tmp_path: Path
    ) -> None:
        """A 2-table project surfaces the FK section in the planner payload."""
        llm = _StaticFKLLM()
        orchestrator = Orchestrator(llm_client=llm)
        sources = {
            "orders": [
                {"customer_id": "c1"},
                {"customer_id": "c2"},
                {"customer_id": "c1"},
                {"customer_id": "c3"},
                {"customer_id": "c2"},
            ],
            "customers": [
                {"id": "c1"},
                {"id": "c2"},
                {"id": "c3"},
            ],
        }
        ctx = self._make_ctx(tmp_path, sources)
        orchestrator.run("Build a sample report", context=ctx)
        assert llm.captured, "LLM stub never invoked"
        # When ``run()`` fails (no tools registered) it retries
        # the planner; the LLM is therefore called more than once
        # and the FK section appears in every retry's payload.
        # Assert on the last call's user message so we catch the
        # state the LLM actually saw before the run bailed.
        user_msg = llm.captured[-1][-1]["content"]
        assert "inferred_foreign_keys" in user_msg, (
            "Planner payload must include the FK section header "
            "when data sources are registered"
        )
        assert "Likely foreign keys (auto-detected)" in user_msg
        # At least one hint pair should appear (the
        # orders.customer_id ↔ customers.id link is the obvious
        # one — distinct_count matches, name suffix strip
        # normalises, sample values overlap).
        assert "orders.customer_id" in user_msg or "customers.id" in user_msg

    def test_disable_flag_omits_fk_section(self, tmp_path: Path) -> None:
        """``fk_inference_enabled=False`` (CLI flag) drops the section."""
        llm = _StaticFKLLM()
        orchestrator = Orchestrator(llm_client=llm)
        sources = {
            "orders": [{"customer_id": "c1"}, {"customer_id": "c2"}],
            "customers": [{"id": "c1"}, {"id": "c2"}],
        }
        ctx = self._make_ctx(tmp_path, sources, fk_enabled=False)
        orchestrator.run("Build a sample report", context=ctx)
        assert llm.captured
        # Same retry caveat as the positive test — assert on the
        # last captured message so we catch the state the LLM
        # actually saw.
        user_msg = llm.captured[-1][-1]["content"]
        assert "inferred_foreign_keys" not in user_msg, (
            "FK section must be omitted when "
            "context['fk_inference_enabled'] is False"
        )

    def test_orchestrator_omits_fk_section_without_data_sources(
        self, tmp_path: Path
    ) -> None:
        """No data sources → no FK section (nothing to inspect)."""
        llm = _StaticFKLLM()
        orchestrator = Orchestrator(llm_client=llm)
        ctx = self._make_ctx(tmp_path, sources={})
        orchestrator.run("Build a sample report", context=ctx)
        assert llm.captured
        user_msg = llm.captured[-1][-1]["content"]
        assert "inferred_foreign_keys" not in user_msg

    def test_fk_section_present_in_planner_payload_directly(
        self, tmp_path: Path
    ) -> None:
        """Direct ``_planner_payload`` call surfaces the FK block.

        Exercises the orchestrator's payload builder without
        running the LLM-driven retry loop — gives a deterministic
        view of what the planner would see on the first
        iteration.
        """
        orchestrator = Orchestrator(llm_client=_StaticFKLLM())
        sources = {
            "orders": [
                {"customer_id": "c1"},
                {"customer_id": "c2"},
                {"customer_id": "c1"},
            ],
            "customers": [
                {"id": "c1"},
                {"id": "c2"},
            ],
        }
        ctx = self._make_ctx(tmp_path, sources)
        payload = orchestrator._planner_payload("Build a report", ctx)
        assert "inferred_foreign_keys" in payload
        block = payload["inferred_foreign_keys"]
        assert block["section_header"] == "## Likely foreign keys (auto-detected)"
        assert block["count"] >= 1
        assert any(
            h["from_column"] == "id"
            and h["to_column"] == "customer_id"
            or h["from_column"] == "customer_id"
            and h["to_column"] == "id"
            for h in block["hints"]
        )
