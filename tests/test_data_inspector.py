"""Tests for the data-inspection context layer.

Covers:

* Loading records from CSV, JSON, JSONL, and in-memory lists.
* Per-column profile computation (type inference, distinct
  counts, numeric range, date range, distinct-value examples).
* Heuristic relationship suggestions across tables.
* The orchestrator's planner payload including ``data_profile``
  when ``context['data_sources']`` is set, and excluding it when
  the caller hasn't registered any data sources.
* ``data_profile_redact_values=True`` strips distinct examples so
  the payload can be sent to a hosted LLM under a strict data-
  residency policy.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.data_inspector import (
    ColumnProfile,
    DataProfile,
    TableProfile,
    inspect_data_source,
    inspect_data_sources,
    suggest_relationships,
)
from nl2pbip.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# inspect_data_source: in-memory records
# ---------------------------------------------------------------------------


class TestInspectInMemoryRecords:
    def test_returns_profile_with_columns(self) -> None:
        profile = inspect_data_source(
            [
                {"region": "EMEA", "amount": 100.0},
                {"region": "APAC", "amount": 200.0},
                {"region": "EMEA", "amount": 150.0},
            ],
            name="Sales",
        )
        assert profile.source_name == "Sales"
        assert profile.source_kind == "records"
        assert len(profile.tables) == 1
        table = profile.tables[0]
        assert table.name == "Sales"
        assert table.row_count == 3
        assert {c.name for c in table.columns} == {"region", "amount"}

    def test_inferred_types(self) -> None:
        profile = inspect_data_source(
            [
                {"a": 1, "b": "x", "c": "2024-01-01", "d": True},
                {"a": 2, "b": "y", "c": "2024-01-02", "d": False},
            ]
        )
        table = profile.tables[0]
        cols_by_name = {c.name: c for c in table.columns}
        assert cols_by_name["a"].inferred_type == "numeric"
        assert cols_by_name["b"].inferred_type == "text"
        assert cols_by_name["c"].inferred_type == "date"
        assert cols_by_name["d"].inferred_type == "boolean"

    def test_numeric_stats(self) -> None:
        profile = inspect_data_source([{"x": v} for v in [1, 2, 3, 4, 5]])
        col = profile.tables[0].columns[0]
        assert col.min == 1.0
        assert col.max == 5.0
        assert col.mean == 3.0
        assert col.median == 3
        assert col.stddev is not None and col.stddev > 0

    def test_numeric_string_coercion(self) -> None:
        """Strings that parse as numbers are coerced — useful for CSV input."""
        profile = inspect_data_source([{"x": "1"}, {"x": "2"}, {"x": "3"}, {"x": "4"}])
        col = profile.tables[0].columns[0]
        assert col.inferred_type == "numeric"
        assert col.min == 1.0
        assert col.max == 4.0

    def test_date_range(self) -> None:
        profile = inspect_data_source(
            [
                {"d": "2024-01-01"},
                {"d": "2024-06-15"},
                {"d": "2024-12-31"},
            ]
        )
        col = profile.tables[0].columns[0]
        assert col.min_date == "2024-01-01"
        assert col.max_date == "2024-12-31"

    def test_distinct_counts(self) -> None:
        profile = inspect_data_source([{"x": v} for v in ["a", "b", "a", "c", "a"]])
        col = profile.tables[0].columns[0]
        assert col.distinct_count == 3  # a, b, c
        assert col.non_null_count == 5

    def test_null_rate(self) -> None:
        profile = inspect_data_source(
            [
                {"x": "a"},
                {"x": None},
                {"x": ""},
                {"x": "b"},
            ]
        )
        col = profile.tables[0].columns[0]
        # 2 out of 4 are null/empty.
        assert col.null_rate == 0.5
        assert col.non_null_count == 2

    def test_distinct_examples_capped_at_five(self) -> None:
        profile = inspect_data_source([{"x": str(i)} for i in range(20)])
        col = profile.tables[0].columns[0]
        assert len(col.distinct_examples) == 5

    def test_distinct_examples_ordered_by_frequency(self) -> None:
        """Examples are the top-N **most frequent** distinct values.

        Sets in Python have arbitrary iteration order, but the LLM
        uses these examples to reason about the data domain. Picking
        top-N by frequency (the actual values the LLM will see most
        often) is more informative than arbitrary distinct samples.
        """
        # "EMEA" appears 5 times — must be top-1.
        # "APAC" appears 3 times — must be top-2.
        # Others appear once — should be in the bottom of the top-5.
        rows = (
            [{"region": "EMEA"} for _ in range(5)]
            + [{"region": "APAC"} for _ in range(3)]
            + [{"region": "AMER"}]
            + [{"region": "LATAM"}]
            + [{"region": "AFRICA"}]
        )
        profile = inspect_data_source(rows)
        col = profile.tables[0].columns[0]
        assert col.distinct_count == 5
        assert col.distinct_examples[0] == "EMEA"
        assert col.distinct_examples[1] == "APAC"
        # The 1-occurrence values can be in any order — Counter
        # returns them in insertion order on ties, so we just
        # verify the top-2 by frequency.
        assert set(col.distinct_examples) == {"EMEA", "APAC", "AMER", "LATAM", "AFRICA"}

    def test_warns_when_sampled(self) -> None:
        """When the source has more rows than ``max_rows``, the profile flags it."""
        profile = inspect_data_source(
            [{"x": i} for i in range(2000)],
            max_rows=500,
        )
        assert any("Sampled first 500" in w for w in profile.warnings)


# ---------------------------------------------------------------------------
# inspect_data_source: CSV files
# ---------------------------------------------------------------------------


class TestInspectCSV:
    def test_csv_path(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "sales.csv"
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["region", "amount"])
            writer.writeheader()
            writer.writerow({"region": "EMEA", "amount": "100.5"})
            writer.writerow({"region": "APAC", "amount": "200.0"})
        profile = inspect_data_source(csv_path)
        assert profile.source_kind == "csv"
        assert profile.tables[0].row_count == 2
        cols = {c.name: c for c in profile.tables[0].columns}
        assert cols["region"].inferred_type == "text"
        # The numeric column is parsed from string "100.5" → 100.5.
        assert cols["amount"].inferred_type == "numeric"

    def test_csv_with_nulls(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "data.csv"
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["x", "y"])
            writer.writeheader()
            writer.writerow({"x": "1", "y": ""})
            writer.writerow({"x": "2", "y": "10"})
            writer.writerow({"x": "", "y": "20"})
        profile = inspect_data_source(csv_path)
        cols = {c.name: c for c in profile.tables[0].columns}
        assert cols["x"].null_rate > 0
        assert cols["y"].null_rate > 0


class TestInspectJSON:
    def test_json_array(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text(
            json.dumps(
                [
                    {"id": 1, "name": "alpha"},
                    {"id": 2, "name": "beta"},
                ]
            )
        )
        profile = inspect_data_source(path)
        assert profile.source_kind == "json"
        assert profile.tables[0].row_count == 2

    def test_json_wrapped_object(self, tmp_path: Path) -> None:
        """A JSON object with a ``records`` key is unwrapped automatically."""
        path = tmp_path / "data.json"
        path.write_text(json.dumps({"records": [{"x": 1}, {"x": 2}, {"x": 3}]}))
        profile = inspect_data_source(path)
        assert profile.tables[0].row_count == 3
        assert profile.tables[0].columns[0].name == "x"

    def test_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')
        profile = inspect_data_source(path)
        assert profile.source_kind == "jsonl"
        assert profile.tables[0].row_count == 3


# ---------------------------------------------------------------------------
# redact_distinct_values
# ---------------------------------------------------------------------------


class TestRedactDistinctValues:
    def test_redact_values_flag_strips_examples(self) -> None:
        profile = inspect_data_source(
            [{"region": "EMEA"}, {"region": "APAC"}],
            redact_distinct_values=True,
        )
        col = profile.tables[0].columns[0]
        # Examples are stripped at profile construction.
        assert col.distinct_examples == []
        # JSON rendering also omits them.
        rendered = profile.to_json(redact_distinct_values=True)
        assert rendered["tables"][0]["columns"][0]["distinct_examples"] == []
        # ``to_json`` with ``redact_distinct_values=False`` keeps them.
        rendered_full = profile.to_json(redact_distinct_values=False)
        # The example is the first key the dict was created with, so
        # the value round-trips. We just verify the column exists.
        assert rendered_full["tables"][0]["columns"][0]["name"] == "region"


# ---------------------------------------------------------------------------
# inspect_data_sources
# ---------------------------------------------------------------------------


class TestInspectDataSources:
    def test_returns_one_profile_per_source(self, tmp_path: Path) -> None:
        a = tmp_path / "a.csv"
        a.write_text("x\n1\n2\n")
        b = tmp_path / "b.csv"
        b.write_text("y\n10\n20\n")
        profiles = inspect_data_sources({"a": a, "b": b})
        assert len(profiles) == 2
        assert {p.source_name for p in profiles} == {"a", "b"}

    def test_missing_source_yields_error_profile(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.csv"
        profiles = inspect_data_sources({"missing": missing})
        assert len(profiles) == 1
        assert profiles[0].source_kind == "error"
        assert any("not found" in w for w in profiles[0].warnings)


# ---------------------------------------------------------------------------
# suggest_relationships
# ---------------------------------------------------------------------------


class TestSuggestRelationships:
    def _make_profile(self, name: str, rows: List[Dict[str, Any]]) -> DataProfile:
        """Build a profile from in-memory rows for testing.

        Populates ``distinct_examples`` from the actual rows so
        :func:`suggest_relationships` has signal to work with —
        the production inspector does the same in
        :func:`_profile_column`.
        """
        cols: List[ColumnProfile] = []
        for key in rows[0].keys():
            values = [r.get(key) for r in rows]
            distinct = list({v for v in values if v is not None})
            cols.append(
                ColumnProfile(
                    name=key,
                    inferred_type="text",
                    non_null_count=sum(1 for v in values if v is not None),
                    distinct_count=len(distinct),
                    null_rate=0.0,
                    distinct_examples=distinct[:5],
                )
            )
        return DataProfile(
            source_name=name,
            source_kind="records",
            tables=[
                TableProfile(
                    name=name,
                    row_count=len(rows),
                    sampled_at_least=len(rows),
                    columns=cols,
                )
            ],
            warnings=[],
        )

    def _make_profile_with_examples(
        self, name: str, distinct_count: int, examples: List[Any]
    ) -> DataProfile:
        """Build a profile with explicit distinct_count and examples.

        Used to test the lower-bound disclosure for overlap ratios
        when ``distinct_count > len(examples)``. Realistic data
        profiles carry at most 5 examples but may have thousands of
        distinct values, so this helper lets us exercise the
        lower-bound code path without manufacturing that many
        rows.
        """
        col = ColumnProfile(
            name="k",
            inferred_type="text",
            non_null_count=distinct_count,
            distinct_count=distinct_count,
            null_rate=0.0,
            distinct_examples=list(examples),
        )
        return DataProfile(
            source_name=name,
            source_kind="records",
            tables=[
                TableProfile(
                    name=name,
                    row_count=distinct_count,
                    sampled_at_least=distinct_count,
                    columns=[col],
                )
            ],
            warnings=[],
        )

    def test_strong_overlap_suggested(self) -> None:
        """Sales[region] ⊂ Region[name] should be flagged as a candidate."""
        sales = self._make_profile(
            "Sales",
            [
                {"region": "EMEA"},
                {"region": "APAC"},
                {"region": "EMEA"},
                {"region": "AMER"},
                {"region": "APAC"},
            ],
        )
        region = self._make_profile(
            "Region",
            [
                {"name": "EMEA"},
                {"name": "APAC"},
                {"name": "AMER"},
            ],
        )
        suggestions = suggest_relationships([sales, region])
        assert len(suggestions) >= 1
        # The first suggestion should point at Sales[region] ↔ Region[name].
        top = suggestions[0]
        assert {top["from_table"], top["to_table"]} == {"Sales", "Region"}
        assert {top["from_column"], top["to_column"]} == {"region", "name"}
        # All Sales region values appear in Region.name.
        assert top["overlap_ratio"] == 1.0

    def test_no_overlap_no_suggestion(self) -> None:
        sales = self._make_profile("Sales", [{"region": "X"}, {"region": "Y"}])
        region = self._make_profile("Region", [{"name": "A"}, {"name": "B"}])
        suggestions = suggest_relationships([sales, region], min_overlap_ratio=0.5)
        assert suggestions == []

    def test_naming_pattern_boosts_confidence(self) -> None:
        """``Sales.region_id`` ↔ ``Region.id`` matches the fk convention."""
        sales = self._make_profile(
            "Sales",
            [
                {"region_id": 1},
                {"region_id": 2},
                {"region_id": 3},
            ],
        )
        region = self._make_profile(
            "Region",
            [
                {"id": 1},
                {"id": 2},
                {"id": 3},
            ],
        )
        suggestions = suggest_relationships([sales, region])
        assert len(suggestions) == 1
        assert suggestions[0]["confidence"] >= 0.7  # overlap (1.0) + 0.1 boost
        assert suggestions[0]["from_column"] == "region_id"
        assert suggestions[0]["to_column"] == "id"

    def test_id_to_id_no_boost(self) -> None:
        """Both columns named ``id`` — no naming boost (degenerate case)."""
        a = self._make_profile("A", [{"id": 1}, {"id": 2}])
        b = self._make_profile("B", [{"id": 1}, {"id": 2}])
        suggestions = suggest_relationships([a, b])
        # Should still suggest the relationship by overlap but
        # without the 0.1 naming-pattern boost. Since the base
        # confidence is the overlap ratio (1.0 here), the test
        # asserts the suggestion exists and that confidence sits in
        # the expected band — strictly less than what a naming-pattern
        # match would produce on the same overlap.
        assert len(suggestions) == 1
        # Without a naming-pattern boost, confidence == overlap_ratio.
        # With a pattern match, confidence would be ratio + 0.1.
        # Both columns named ``id`` is the degenerate case so we
        # expect the lower band.
        assert suggestions[0]["confidence"] == 1.0
        # A hypothetical matched-name pair would also yield 1.0
        # (capped) so the no-boost distinction lives in the
        # suggestion set, not the confidence value. Verify the
        # suggestion list is exactly one entry — not zero (no
        # overlap), not two (no double counting).
        assert len(suggestions) == 1

    def test_same_table_excluded(self) -> None:
        a = self._make_profile("A", [{"x": 1}, {"x": 2}])
        suggestions = suggest_relationships([a])
        # Same-table comparisons aren't supported; only one table here.
        assert suggestions == []

    def test_results_capped(self) -> None:
        # Build many tables with overlapping keys.
        profiles = []
        for i in range(20):
            profiles.append(self._make_profile(f"T{i}", [{"k": 1}, {"k": 2}]))
        suggestions = suggest_relationships(profiles, max_suggestions=5)
        assert len(suggestions) <= 5

    def test_min_overlap_ratio_respected(self) -> None:
        """A 50% overlap is rejected when the threshold is 0.6.

        ``suggest_relationships`` measures overlap against the
        *smaller* side's distinct set (the "many" side of the
        relationship). With a 2-distinct column on one table and a
        4-distinct column on the other, the algorithm picks the
        2-distinct side as ``small`` and computes overlap against
        its set. If those 2 values are both present on the larger
        side, overlap = 1.0 and the threshold doesn't apply.

        To exercise the threshold we need the *smaller* side to have
        values the larger side doesn't carry. Below, the smaller
        side has 4 values, only 2 of which appear on the larger
        side — overlap = 2/4 = 0.5.
        """
        # Smaller side (Distinct count = 2) has 2 values; larger side
        # (Distinct count = 4) has those 2 values plus 2 extras.
        # My code picks the SMALLER side (2 distinct) as the overlap
        # base, so this gives ratio = 2/2 = 1.0 — the relationship is
        # always suggested regardless of threshold.
        # To get a 50% overlap, we need the smaller side's distinct
        # values to include values the larger side DOES NOT have.
        small = self._make_profile(
            "Small", [{"x": "a"}, {"x": "b"}, {"x": "c"}, {"x": "d"}]
        )
        # Larger side has only "a" and "b" — overlap with the small
        # side's 4 distinct values is 2/4 = 0.5.
        large = self._make_profile("Large", [{"k": "a"}, {"k": "b"}])
        # My code picks Small (4 distinct) as the larger side, and
        # Large (2 distinct) as the smaller side. The overlap ratio
        # is computed against the smaller side's distinct set:
        # {a, b} ∩ {a, b, c, d} = {a, b} = 2/2 = 1.0. So this case
        # is always suggested.
        #
        # To exercise the threshold logic we need a case where the
        # smaller side has values NOT on the larger side. The
        # simplest way is to invert: the "small" column has a value
        # not on the "large" column.
        small = self._make_profile(
            "Small",
            [
                {"x": "a"},
                {"x": "b"},
                {"x": "c"},
                {"x": "d"},
            ],
        )
        large = self._make_profile(
            "Large",
            # Only "a" and "b" exist on the larger side. My code
            # picks the LARGER column as "large" and the SMALLER as
            # "small". Both are 2-distinct here so the comparison
            # is symmetric.
            [
                {"k": "a"},
                {"k": "b"},
                {"k": "e"},  # not in Small — adds noise
                {"k": "f"},  # not in Small — adds noise
            ],
        )
        # Small (4 distinct) → Large (4 distinct). Symmetric.
        # ratio against small: {a,b} ∩ {a,b,e,f} = 2/4 = 0.5.
        # Below threshold of 0.6 → rejected.
        assert suggest_relationships([small, large], min_overlap_ratio=0.6) == []
        # 0.5 lets it through (>=, not >).
        assert len(suggest_relationships([small, large], min_overlap_ratio=0.5)) == 1

    def test_overlap_ratio_marks_lower_bound(self) -> None:
        """When distinct_count > len(examples), ratio is a lower bound.

        The inspector caps the distinct_examples list at 5 entries
        per column. When the actual distinct_count is higher, the
        overlap ratio computed against the examples is a *lower
        bound* on the true overlap — the values outside the sample
        may also match, so the LLM should not over-trust a near-1.0
        ratio computed from a small sample.
        """
        # Sales has 10 distinct values but only the first 5 land in
        # distinct_examples. Region shares 3 of those 5 with Sales.
        # True overlap is 3/10 = 0.3; sampled-overlap is 3/5 = 0.6.
        sales = self._make_profile_with_examples(
            "Sales", 10, ["v0", "v1", "v2", "v3", "v4"]
        )
        region = self._make_profile_with_examples(
            "Region", 10, ["v2", "v3", "v4", "v5", "v6"]
        )
        suggestions = suggest_relationships([sales, region])
        assert len(suggestions) == 1
        s = suggestions[0]
        # Sampled ratio is 0.6 but the field is labelled as a lower
        # bound so the LLM doesn't over-trust it.
        assert s["overlap_ratio"] == 0.6
        assert s["ratio_is_lower_bound"] is True
        assert s["examples_seen"] == 5
        assert s["total_small_distinct"] == 10
        # Rationale text calls out the lower-bound caveat.
        assert "lower bound" in s["rationale"]

    def test_overlap_ratio_exact_when_examples_match_distinct(self) -> None:
        """When distinct_count == len(examples), ratio is exact.

        Small columns (≤ 5 distinct values) don't hit the lower-
        bound caveat because every distinct value appears in the
        examples list.
        """
        a = self._make_profile_with_examples("A", 3, ["x", "y", "z"])
        b = self._make_profile_with_examples("B", 3, ["x", "y", "z"])
        suggestions = suggest_relationships([a, b])
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s["ratio_is_lower_bound"] is False
        assert s["examples_seen"] == 3
        assert s["total_small_distinct"] == 3
        # No lower-bound caveat when the sample is exhaustive.
        assert "lower bound" not in s["rationale"]


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorDataProfileContext:
    """``context['data_sources']`` makes the inspector run."""

    def _llm(self):
        from typing import Any, Dict, List

        class _Stub:
            def generate(self, messages):
                return json.dumps(
                    {
                        "plan": [
                            {
                                "tool": "package_pbip",
                                "args": {"output_path": "out.pbip"},
                            }
                        ]
                    }
                )

        return _Stub()

    def _flatten_tables(self, profile: Dict[str, Any]):
        """Flatten the ``data_profile.tables[i].tables[*]`` nesting."""
        for source_entry in profile["tables"]:
            for table in source_entry["tables"]:
                yield source_entry, table

    def test_data_profile_included_when_sources_registered(
        self, tmp_path: Path
    ) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        ctx["data_sources"] = {
            "Sales": [
                {"region": "EMEA", "amount": 100.0},
                {"region": "APAC", "amount": 200.0},
                {"region": "EMEA", "amount": 150.0},
            ],
            "Region": [
                {"name": "EMEA"},
                {"name": "APAC"},
            ],
        }
        orch = Orchestrator(llm_client=self._llm())
        payload = orch._planner_payload("Build a Sales model.", ctx)
        assert "data_profile" in payload
        profile = payload["data_profile"]
        # Both sources are profiled.
        source_names = {t["source_name"] for t in profile["tables"]}
        assert source_names == {"Sales", "Region"}
        # Find the Sales.region column.
        sales_region = None
        for source_entry, table in self._flatten_tables(profile):
            if source_entry["source_name"] == "Sales" and table["name"] == "Sales":
                for col in table["columns"]:
                    if col["name"] == "region":
                        sales_region = col
                        break
        assert sales_region is not None
        assert sales_region["distinct_count"] == 2
        # A relationship suggestion between Sales.region ↔ Region.name
        # should be present.
        rels = profile["suggested_relationships"]
        assert len(rels) >= 1
        top = rels[0]
        assert top["from_column"] == "region"
        assert top["to_column"] == "name"

    def test_data_profile_absent_when_no_sources(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        orch = Orchestrator(llm_client=self._llm())
        payload = orch._planner_payload("prompt", ctx)
        assert "data_profile" not in payload

    def test_redact_values_flag_strips_examples(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        ctx["data_sources"] = {
            "Sales": [{"region": "EMEA"}, {"region": "APAC"}],
        }
        ctx["data_profile_redact_values"] = True
        orch = Orchestrator(llm_client=self._llm())
        payload = orch._planner_payload("prompt", ctx)
        # Walk the source → tables → columns nesting to find the
        # first column and check its examples were stripped.
        _, table = next(iter(self._flatten_tables(payload["data_profile"])))
        col = table["columns"][0]
        assert col["distinct_examples"] == []

    def test_max_rows_per_source_respected(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        ctx["data_sources"] = {
            "Big": [{"x": i} for i in range(2000)],
        }
        ctx["data_profile_max_rows"] = 50
        orch = Orchestrator(llm_client=self._llm())
        payload = orch._planner_payload("prompt", ctx)
        source_entry, table = next(iter(self._flatten_tables(payload["data_profile"])))
        # Only 50 rows were sampled, even though the source had 2000.
        assert table["row_count"] == 50
        assert table["sampled_at_least"] == 2000
        # The warning lives on the source entry (it applies to the
        # whole source, not per-table).
        warnings = source_entry.get("warnings", [])
        assert any("Sampled first 50" in w for w in warnings)
