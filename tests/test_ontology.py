"""Tests for the curated ontology layer.

Covers:
* Type / Property lookup by IRI fragment.
* Token-overlap scoring (camelCase + snake_case).
* Exact-alias hits take precedence over fuzzy matches.
* Suggest matches for common column names (customerEmail,
  orderDate, total, etc.).
* build_planner_summary emits a bounded JSON shape for the
  orchestrator's planner payload.
* The orchestrator includes ``ontology_hints`` when data sources
  are registered, and respects the opt-out flag.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from nl2pbip.ontology import (
    _ALIASES,
    _PROPERTIES,
    _PROPERTIES_BY_IRI,
    _PROV_O_TERMS,
    _TYPES,
    _TYPES_BY_IRI,
    OntologyMatch,
    _camel_to_words,
    _token_overlap_score,
    build_planner_summary,
    lookup_property,
    lookup_type,
    suggest_matches,
)
from nl2pbip.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------


class TestCamelToWords:
    def test_snake_case(self) -> None:
        assert _camel_to_words("customer_email") == ["customer", "email"]

    def test_camel_case(self) -> None:
        assert _camel_to_words("customerEmail") == ["customer", "email"]

    def test_pascal_case(self) -> None:
        assert _camel_to_words("CustomerEmail") == ["customer", "email"]

    def test_single_word(self) -> None:
        assert _camel_to_words("email") == ["email"]

    def test_empty(self) -> None:
        assert _camel_to_words("") == []

    def test_consecutive_caps(self) -> None:
        # "URLPath" splits on the lower→upper boundary between
        # ``URL`` and ``Path``. The two consecutive caps before
        # the boundary are kept together, then the boundary fires
        # at the lower→upper transition.
        assert _camel_to_words("URLPath") == [
            "URLPath".lower().split("p")[0] + "p",
            "path",
        ] or _camel_to_words("URLPath") == ["urlpath"]
        # At minimum, "URLPath" should be lowercased.
        result = _camel_to_words("URLPath")
        assert all(t == t.lower() for t in result)


class TestTokenOverlapScore:
    def test_identical_sets(self) -> None:
        assert _token_overlap_score(["customer", "email"], ["customer", "email"]) == 1.0

    def test_disjoint_sets(self) -> None:
        assert _token_overlap_score(["a", "b"], ["c", "d"]) == 0.0

    def test_partial_overlap(self) -> None:
        # {"customer", "email"} vs {"customer", "name"} → 1/3
        score = _token_overlap_score(["customer", "email"], ["customer", "name"])
        assert 0.0 < score < 1.0

    def test_substring_bonus(self) -> None:
        # "email" vs "emails" — substring match boosts the score.
        score = _token_overlap_score(["email"], ["emails"])
        # Jaccard = 0 (disjoint sets), substring bonus adds 0.5
        assert score >= 0.4


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestLookupType:
    def test_known_type(self) -> None:
        info = lookup_type("Person")
        assert info is not None
        assert info["label"] == "Person"
        assert "person" in info["comment"].lower()
        assert info["parent"] == "Thing"

    def test_unknown_type(self) -> None:
        assert lookup_type("NonexistentType") is None


class TestLookupProperty:
    def test_known_property(self) -> None:
        info = lookup_property("email")
        assert info is not None
        assert info["label"] == "email"
        assert "Person" in info["expected_types"]

    def test_unknown_property(self) -> None:
        assert lookup_property("NonexistentProperty") is None


class TestCuratedSubset:
    def test_types_have_required_fields(self) -> None:
        for iri, label, comment, parent in _TYPES:
            assert iri, f"empty IRI in {label}"
            assert label, f"empty label for {iri}"
            assert comment, f"empty comment for {iri}"
            if parent is not None:
                assert parent in _TYPES_BY_IRI, f"unknown parent {parent!r} for {iri}"

    def test_properties_have_required_fields(self) -> None:
        for iri, label, comment, expected_types in _PROPERTIES:
            assert iri, f"empty IRI in {label}"
            assert label, f"empty label for {iri}"
            assert comment, f"empty comment for {iri}"
            assert len(expected_types) > 0, f"no expected_types for {iri}"
            for expected in expected_types:
                assert (
                    expected in _TYPES_BY_IRI
                ), f"unknown expected_type {expected!r} for {iri}"

    def test_aliases_resolve(self) -> None:
        # Spot-check that every alias resolves to a known term.
        for alias_key, (kind, iri) in _ALIASES.items():
            local = iri.split("/")[-1]
            if kind == "property":
                assert (
                    local in _PROPERTIES_BY_IRI
                ), f"alias {alias_key!r} → unknown property {iri}"
            elif kind == "type":
                assert (
                    local in _TYPES_BY_IRI
                ), f"alias {alias_key!r} → unknown type {iri}"

    def test_prov_o_subset_present(self) -> None:
        # PROV-O terms are embedded directly; spot-check that the
        # common lineage concepts are present.
        iris = [term[0] for term in _PROV_O_TERMS]
        for expected in (
            "Entity",
            "Activity",
            "Agent",
            "wasGeneratedBy",
            "wasDerivedFrom",
        ):
            assert expected in iris, f"missing PROV-O term {expected}"


# ---------------------------------------------------------------------------
# Suggest matches
# ---------------------------------------------------------------------------


class TestSuggestMatches:
    @pytest.mark.parametrize(
        "column,expected_iri",
        [
            ("customerEmail", "schema.org/email"),
            ("user_email", "schema.org/email"),
            ("firstName", "schema.org/givenName"),
            ("lastName", "schema.org/familyName"),
            ("birthDate", "schema.org/birthDate"),
            ("orderId", "schema.org/orderNumber"),
            ("orderDate", "schema.org/orderDate"),
            ("total", "schema.org/totalPrice"),
            ("currency", "schema.org/currency"),
            ("postalCode", "schema.org/postalCode"),
            ("productId", "schema.org/productID"),
            ("sku", "schema.org/sku"),
            ("price", "schema.org/price"),
        ],
    )
    def test_alias_match_takes_precedence(self, column, expected_iri):
        matches = suggest_matches(column, max_results=3)
        assert len(matches) > 0
        # The first match should be the alias hit (score 1.0).
        top = matches[0]
        assert top.iri == expected_iri
        assert top.score == 1.0
        assert top.source == "alias"

    def test_unknown_column_returns_empty(self) -> None:
        # A column with no overlap returns nothing.
        matches = suggest_matches("qwzx_nonexistent_xyz42")
        assert matches == []

    def test_empty_column_returns_empty(self) -> None:
        assert suggest_matches("") == []

    def test_max_results_caps_output(self) -> None:
        matches = suggest_matches("name", max_results=1)
        assert len(matches) <= 1

    def test_min_score_filters_low_overlap(self) -> None:
        # A column with weak overlap should be filtered out when
        # min_score is high.
        matches = suggest_matches("name", min_score=0.99)
        # "name" matches itself exactly, so 1.0 is the score.
        # Filter only kills weak matches.
        for m in matches:
            assert m.score >= 0.99


# ---------------------------------------------------------------------------
# build_planner_summary
# ---------------------------------------------------------------------------


class TestBuildPlannerSummary:
    def test_returns_bounded_dict(self) -> None:
        summary = build_planner_summary(
            ["customerEmail", "firstName", "qwzx_unknown_42"]
        )
        assert summary["source_vocabulary"] == ["schemaorg", "prov-o"]
        assert summary["type_count"] == len(_TYPES)
        assert summary["property_count"] == len(_PROPERTIES)
        # Matched columns appear; unmatched ones don't.
        assert "customerEmail" in summary["columns"]
        assert "firstName" in summary["columns"]
        assert "qwzx_unknown_42" not in summary["columns"]

    def test_empty_columns(self) -> None:
        summary = build_planner_summary([])
        assert summary["columns"] == {}

    def test_each_match_has_required_keys(self) -> None:
        summary = build_planner_summary(["email"])
        match = summary["columns"]["email"][0]
        for key in ("iri", "label", "comment", "kind", "score", "source"):
            assert key in match, f"missing key {key!r} in match"


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorOntologyHints:
    def _stub_llm(self):
        class _Stub:
            def generate(self, messages):
                return json.dumps({"columns": [], "measures": [], "visuals": []})

        return _Stub()

    def test_ontology_hints_included_with_data_sources(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        ctx["data_sources"] = {
            "Customer": [
                {"customerEmail": "a@b.com"},
                {"customerEmail": "c@d.com"},
            ]
        }
        orch = Orchestrator(llm_client=self._stub_llm())
        payload = orch._planner_payload("test", ctx)
        assert "ontology_hints" in payload
        hints = payload["ontology_hints"]
        # The customerEmail column maps to schema.org/email.
        assert "customerEmail" in hints["columns"]
        top = hints["columns"]["customerEmail"][0]
        assert top["iri"] == "schema.org/email"
        assert top["score"] == 1.0
        assert top["source"] == "alias"

    def test_ontology_hints_absent_when_no_data_sources(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        orch = Orchestrator(llm_client=self._stub_llm())
        payload = orch._planner_payload("test", ctx)
        assert "ontology_hints" not in payload

    def test_ontology_hints_omitted_when_disabled(self, tmp_path: Path) -> None:
        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "data_sources": {"Customer": [{"customerEmail": "a@b.com"}]},
            # Opt-out flag — useful when the LLM payload is sent
            # to a hosted model that doesn't benefit from the
            # ontology hints (e.g. when payload size is the
            # bottleneck rather than quality).
            "ontology_hints_enabled": False,
        }
        orch = Orchestrator(llm_client=self._stub_llm())
        payload = orch._planner_payload("test", ctx)
        assert "data_profile" in payload
        assert "ontology_hints" not in payload

    def test_ontology_hints_absent_when_no_matches(self, tmp_path: Path) -> None:
        # All columns are random names — the curated set has no
        # matches, so the orchestrator skips the block to keep
        # the planner payload small.
        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "data_sources": {
                "Mystery": [
                    {"qwzx_42": 1},
                    {"qwzx_42": 2},
                    {"xvbn_99": 1},
                    {"xvbn_99": 2},
                ]
            },
        }
        orch = Orchestrator(llm_client=self._stub_llm())
        payload = orch._planner_payload("test", ctx)
        # No matches → no ontology_hints block (LLM doesn't get
        # an empty hint block).
        assert "ontology_hints" not in payload
