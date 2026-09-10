"""Tests for cross-table data understanding.

Covers:
* Primary-key detection from per-column statistics.
* FK coverage stats (matching rows, orphan rows, coverage ratio).
* Cardinality hint logic (oneToOne / manyToOne / manyToMany).
* Numeric quantile computation (P25/P50/P75/P95) and skew heuristic.
* Time range computation for date columns.
* Top-level ``analyze_data_understanding`` returns the right shape
  on a realistic dataset.
* Orchestrator integration: ``data_understanding`` block is
  emitted alongside ``data_profile`` when data sources are
  registered, and respects the opt-out flag.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.data_inspector import inspect_data_sources
from nl2pbip.data_understanding import (
    DataUnderstanding,
    NumericDistribution,
    PrimaryKeyCandidate,
    RelationshipCoverage,
    TimeRange,
    analyze_data_understanding,
    compute_numeric_distribution,
    compute_numeric_quantiles,
    compute_time_range,
    detect_primary_key_candidates,
    verify_relationship_coverage,
)
from nl2pbip.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Primary-key detection
# ---------------------------------------------------------------------------


def _profile_with(table_name: str, columns: List[Dict[str, Any]]) -> Any:
    """Build a minimal DataProfile with the given column stats."""
    from nl2pbip.data_inspector import (
        ColumnProfile,
        DataProfile,
        TableProfile,
    )

    table = TableProfile(
        name=table_name,
        row_count=10,
        sampled_at_least=10,
        columns=[
            ColumnProfile(
                name=col["name"],
                inferred_type=col.get("inferred_type", "text"),
                non_null_count=col.get("non_null_count", 10),
                distinct_count=col["distinct_count"],
                null_rate=col.get("null_rate", 0.0),
                distinct_examples=col.get("examples", []),
            )
            for col in columns
        ],
    )
    return DataProfile(
        source_name=table_name,
        source_kind="records",
        tables=[table],
    )


class TestPrimaryKeyDetection:
    def test_perfect_uniqueness_is_strong(self) -> None:
        profile = _profile_with("T", [{"name": "id", "distinct_count": 10}])
        candidates = detect_primary_key_candidates(profile)
        assert len(candidates) == 1
        assert candidates[0].column == "id"
        assert candidates[0].confidence == "strong"
        assert candidates[0].distinct_ratio == 1.0

    def test_high_cardinality_text_is_likely(self) -> None:
        # 95% unique, text column — "likely" confidence.
        profile = _profile_with(
            "T", [{"name": "email", "distinct_count": 19, "row_count": 20}]
        )
        # Adjust row_count via a fresh profile
        from nl2pbip.data_inspector import (
            ColumnProfile,
            DataProfile,
            TableProfile,
        )

        profile = DataProfile(
            source_name="T",
            source_kind="records",
            tables=[
                TableProfile(
                    name="T",
                    row_count=20,
                    sampled_at_least=20,
                    columns=[
                        ColumnProfile(
                            name="email",
                            inferred_type="text",
                            non_null_count=20,
                            distinct_count=19,
                            null_rate=0.0,
                        )
                    ],
                )
            ],
        )
        candidates = detect_primary_key_candidates(profile)
        assert len(candidates) == 1
        assert candidates[0].confidence == "likely"

    def test_low_cardinality_not_pk(self) -> None:
        profile = _profile_with(
            "T", [{"name": "region", "distinct_count": 3, "row_count": 100}]
        )
        candidates = detect_primary_key_candidates(profile)
        assert candidates == []


# ---------------------------------------------------------------------------
# Relationship coverage
# ---------------------------------------------------------------------------


class TestRelationshipCoverage:
    def test_perfect_coverage(self) -> None:
        from_records = [
            {"customerEmail": "a@x"},
            {"customerEmail": "b@x"},
            {"customerEmail": "a@x"},
            {"customerEmail": "b@x"},
        ]
        to_records = [
            {"customerEmail": "a@x"},
            {"customerEmail": "b@x"},
        ]
        cov = verify_relationship_coverage(
            from_records, "customerEmail", to_records, "customerEmail"
        )
        assert cov is not None
        assert cov.total_rows == 4
        assert cov.matching_rows == 4
        assert cov.orphan_rows == 0
        assert cov.coverage_ratio == 1.0

    def test_partial_coverage_with_orphans(self) -> None:
        from_records = [
            {"customerEmail": "a@x"},
            {"customerEmail": "ghost@x"},
            {"customerEmail": "b@x"},
            {"customerEmail": "a@x"},
        ]
        to_records = [
            {"customerEmail": "a@x"},
            {"customerEmail": "b@x"},
        ]
        cov = verify_relationship_coverage(
            from_records, "customerEmail", to_records, "customerEmail"
        )
        assert cov is not None
        assert cov.total_rows == 4
        assert cov.matching_rows == 3
        assert cov.orphan_rows == 1
        assert cov.coverage_ratio == 0.75

    def test_cardinality_one_to_one(self) -> None:
        # Same number of distinct values on both sides + perfect
        # coverage → oneToOne.
        from_records = [
            {"id": 1},
            {"id": 2},
            {"id": 3},
        ]
        to_records = [
            {"id": 1},
            {"id": 2},
            {"id": 3},
        ]
        cov = verify_relationship_coverage(from_records, "id", to_records, "id")
        assert cov is not None
        assert cov.cardinality_hint == "oneToOne"

    def test_cardinality_many_to_one(self) -> None:
        # Many FK values point to one PK value — manyToOne.
        from_records = [
            {"parent_id": 1},
            {"parent_id": 1},
            {"parent_id": 1},
        ]
        to_records = [{"id": 1}, {"id": 2}, {"id": 3}]
        cov = verify_relationship_coverage(from_records, "parent_id", to_records, "id")
        assert cov is not None
        assert cov.cardinality_hint == "manyToOne"

    def test_empty_records_returns_none(self) -> None:
        cov = verify_relationship_coverage([], "id", [{"id": 1}], "id")
        assert cov is None


# ---------------------------------------------------------------------------
# Numeric quantiles / distribution
# ---------------------------------------------------------------------------


class TestNumericQuantiles:
    def test_basic_quantiles(self) -> None:
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        p25, p50, p75, p95 = compute_numeric_quantiles(values)
        assert p25 == 3.25
        assert p50 == 5.5
        assert p75 == 7.75
        # P95 of 10 items — interpolation between indices 8 and 9
        # at rank = 0.95 * 9 = 8.55: 9 + (10 - 9) * 0.55 = 9.55
        assert p95 == pytest.approx(9.55)

    def test_empty_returns_none(self) -> None:
        assert compute_numeric_quantiles([]) is None

    def test_distribution_skew_left(self) -> None:
        # Long left tail — median closer to P75.
        values = [1, 1, 1, 1, 1, 1, 1, 2, 3, 100]
        dist = compute_numeric_distribution("T", "x", values)
        assert dist is not None
        assert dist.skew == "left"

    def test_distribution_skew_right(self) -> None:
        # Long right tail — median closer to P25.
        values = [1, 2, 3, 100, 100, 100, 100, 100, 100, 100]
        dist = compute_numeric_distribution("T", "x", values)
        assert dist is not None
        assert dist.skew == "right"

    def test_too_few_values_returns_none(self) -> None:
        # <5 values → not enough sample for quantiles.
        assert compute_numeric_distribution("T", "x", [1, 2, 3]) is None

    def test_outlier_count_above_p95(self) -> None:
        # 1 in 20 should be above P95.
        values = list(range(1, 21))  # 1..20
        dist = compute_numeric_distribution("T", "x", values)
        assert dist is not None
        # P95 of 1..20: rank = 0.95 * 19 = 18.05, value = 19 + (20-19)*0.05 = 19.05
        # Values > 19.05: just 20
        assert dist.likely_outliers == 1


# ---------------------------------------------------------------------------
# Time range
# ---------------------------------------------------------------------------


class TestTimeRange:
    def test_basic_time_range(self) -> None:
        values = ["2024-01-15", "2024-02-20", "2024-03-10", "2024-04-05"]
        tr = compute_time_range("T", "d", values)
        assert tr is not None
        assert tr.min_date == "2024-01-15"
        assert tr.max_date == "2024-04-05"
        assert tr.distinct_dates == 4

    def test_iso_datetime_truncated_to_date(self) -> None:
        values = ["2024-01-15T10:30:00", "2024-02-20T08:00:00"]
        tr = compute_time_range("T", "d", values)
        assert tr is not None
        assert tr.min_date == "2024-01-15"
        assert tr.max_date == "2024-02-20"

    def test_too_few_dates_returns_none(self) -> None:
        assert compute_time_range("T", "d", ["2024-01-15"]) is None

    def test_non_string_values_skipped(self) -> None:
        # Numbers aren't dates — all skipped.
        assert compute_time_range("T", "d", [1, 2, 3]) is None


# ---------------------------------------------------------------------------
# Top-level analyzer
# ---------------------------------------------------------------------------


class TestAnalyzeDataUnderstanding:
    def test_realistic_dataset(self) -> None:
        sources = {
            "Customer": [
                {"customerEmail": "alice@bigco.com"},
                {"customerEmail": "bob@bigco.com"},
                {"customerEmail": "carol@bigco.com"},
            ],
            "Order": [
                # 8 orders so numeric distribution has enough
                # samples (>=5) to produce meaningful quantiles.
                {
                    "orderId": "A1",
                    "total": 99.99,
                    "orderDate": "2024-01-15",
                    "customerEmail": "alice@bigco.com",
                },
                {
                    "orderId": "A2",
                    "total": 149.50,
                    "orderDate": "2024-02-20",
                    "customerEmail": "alice@bigco.com",
                },
                {
                    "orderId": "A3",
                    "total": 200.00,
                    "orderDate": "2024-03-10",
                    "customerEmail": "ghost@bigco.com",
                },
                {
                    "orderId": "A4",
                    "total": 75.00,
                    "orderDate": "2024-03-25",
                    "customerEmail": "alice@bigco.com",
                },
                {
                    "orderId": "A5",
                    "total": 350.00,
                    "orderDate": "2024-04-08",
                    "customerEmail": "bob@bigco.com",
                },
                {
                    "orderId": "A6",
                    "total": 120.00,
                    "orderDate": "2024-04-22",
                    "customerEmail": "alice@bigco.com",
                },
                {
                    "orderId": "A7",
                    "total": 480.00,
                    "orderDate": "2024-05-10",
                    "customerEmail": "carol@bigco.com",
                },
                {
                    "orderId": "A8",
                    "total": 50.00,
                    "orderDate": "2024-05-30",
                    "customerEmail": "bob@bigco.com",
                },
            ],
        }
        profiles = inspect_data_sources(sources)
        understanding = analyze_data_understanding(profiles, records_by_source=sources)
        # PK: Customer.customerEmail and Order.orderId are both strong.
        pk_columns = {(pk.table, pk.column) for pk in understanding.primary_keys}
        assert ("Customer", "customerEmail") in pk_columns
        assert ("Order", "orderId") in pk_columns
        # FK coverage: Order.customerEmail → Customer.customerEmail.
        # 8 orders, 1 orphan → coverage = 7/8 = 0.875.
        fk = next(
            (
                rc
                for rc in understanding.relationship_coverage
                if rc.from_table == "Order"
                and rc.to_table == "Customer"
                and rc.from_column == "customerEmail"
            ),
            None,
        )
        assert fk is not None
        assert fk.matching_rows == 7
        assert fk.orphan_rows == 1
        assert fk.coverage_ratio == pytest.approx(7 / 8)
        # Numeric distribution for Order.total.
        dist = next(
            (
                d
                for d in understanding.numeric_distributions
                if d.table == "Order" and d.column == "total"
            ),
            None,
        )
        assert dist is not None
        # P50 of [50, 75, 99.99, 120, 149.5, 200, 350, 480] lands
        # at index 3.5 of the sorted list (between 120 and 149.5).
        assert dist.p50 == pytest.approx(134.75)
        # Time range for Order.orderDate.
        tr = next(
            (
                t
                for t in understanding.time_ranges
                if t.table == "Order" and t.column == "orderDate"
            ),
            None,
        )
        assert tr is not None
        assert tr.min_date == "2024-01-15"
        assert tr.max_date == "2024-05-30"

    def test_no_records_no_fk_coverage(self) -> None:
        # Without records_by_source, FK coverage is empty.
        sources = {"Order": [{"customerEmail": "x"}]}
        profiles = inspect_data_sources(sources)
        understanding = analyze_data_understanding(profiles)
        # PK detection still works from profile stats.
        assert understanding.relationship_coverage == []

    def test_empty_profiles_returns_empty(self) -> None:
        understanding = analyze_data_understanding([])
        assert understanding.to_json() == {
            "primary_keys": [],
            "relationship_coverage": [],
            "numeric_distributions": [],
            "time_ranges": [],
            "warnings": [],
        }


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorDataUnderstanding:
    """``data_understanding`` block in the planner payload."""

    def _stub(self):
        import json

        class _Stub:
            def generate(self, messages):
                return json.dumps({"columns": [], "measures": [], "visuals": []})

        return _Stub()

    def test_emitted_with_data_sources(self, tmp_path: Path) -> None:
        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "data_sources": {
                "Customer": [{"customerEmail": "alice@x"}, {"customerEmail": "bob@x"}],
                "Order": [
                    {"orderId": "A1", "customerEmail": "alice@x"},
                    {"orderId": "A2", "customerEmail": "ghost@x"},  # orphan
                ],
            },
        }
        orch = Orchestrator(llm_client=self._stub())
        payload = orch._planner_payload("test", ctx)
        assert "data_understanding" in payload
        understanding = payload["data_understanding"]
        # PK: Customer.customerEmail + Order.orderId both flagged.
        pks = {(pk["table"], pk["column"]) for pk in understanding["primary_keys"]}
        assert ("Customer", "customerEmail") in pks
        assert ("Order", "orderId") in pks
        # FK coverage: at least one entry with Order → Customer.
        fks = [
            fk
            for fk in understanding["relationship_coverage"]
            if fk["from_table"] == "Order" and fk["to_table"] == "Customer"
        ]
        assert len(fks) >= 1
        assert fks[0]["orphan_rows"] == 1
        assert fks[0]["matching_rows"] == 1

    def test_omitted_when_no_data_sources(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        orch = Orchestrator(llm_client=self._stub())
        payload = orch._planner_payload("test", ctx)
        assert "data_understanding" not in payload

    def test_omitted_when_disabled(self, tmp_path: Path) -> None:
        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "data_sources": {"Order": [{"customerEmail": "x"}]},
            "data_understanding_enabled": False,
        }
        orch = Orchestrator(llm_client=self._stub())
        payload = orch._planner_payload("test", ctx)
        assert "data_understanding" not in payload
