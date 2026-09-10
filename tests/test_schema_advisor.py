"""Tests for the AI schema advisor.

Covers:

* ``SchemaAdvisor.advise`` builds a deterministic prompt from the
  deterministic profile and parses the LLM's JSON response.
* Tolerant parsing handles code-fenced output, leading prose, and
  minor JSON malformations.
* The advisor returns ``None`` when the LLM call fails or the
  response is unrecoverable.
* Results are cached by structural fingerprint so the same data
  shape doesn't trigger a second LLM call.
* The orchestrator includes ``ai_schema_hints`` in the planner
  payload when an LLM client is wired up.
* Callers can opt out via ``context["data_inspector_ai_enabled"]``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.data_inspector import (
    ColumnProfile,
    DataProfile,
    TableProfile,
    inspect_data_source,
)
from nl2pbip.orchestrator import Orchestrator
from nl2pbip.schema_advisor import (
    ColumnSemantics,
    MeasureSuggestion,
    SchemaAdvisor,
    SchemaAdvisorResult,
    VisualSuggestion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_llm(reply: str):
    """Build a stub LLM that returns ``reply`` on every ``generate`` call."""

    class _Stub:
        def generate(self, messages):
            return reply

    return _Stub()


def _build_profiles() -> List[DataProfile]:
    """Realistic profiles: Sales + Region + Date."""
    sales = inspect_data_source(
        [
            {"Region": "EMEA", "Amount": 100.0 + i, "Date": "2024-01-15"}
            for i in range(50)
        ]
        + [
            {"Region": "APAC", "Amount": 200.0 + i, "Date": "2024-02-15"}
            for i in range(50)
        ]
        + [
            {"Region": "AMER", "Amount": 300.0 + i, "Date": "2024-03-15"}
            for i in range(50)
        ]
        + [
            {"Region": "LATAM", "Amount": 400.0 + i, "Date": "2024-04-15"}
            for i in range(50)
        ],
        name="Sales",
    )
    region = inspect_data_source(
        [{"Name": n} for n in ["EMEA", "APAC", "AMER", "LATAM"]],
        name="Region",
    )
    return [sales, region]


VALID_LLM_RESPONSE = json.dumps(
    {
        "columns": [
            {
                "table": "Sales",
                "name": "Region",
                "role": "dimension",
                "description": "Sales region code (EMEA, APAC, etc.).",
                "suggested_measure_name": None,
            },
            {
                "table": "Sales",
                "name": "Amount",
                "role": "measure",
                "description": "Sale amount in USD.",
                "suggested_measure_name": "Total Amount",
            },
            {
                "table": "Region",
                "name": "Name",
                "role": "dimension",
                "description": "Region name (unique).",
                "suggested_measure_name": None,
            },
        ],
        "measures": [
            {
                "table": "Sales",
                "name": "Total Revenue",
                "expression": "SUM(Sales[Amount])",
                "format_string": "$#,0.00",
                "rationale": "Sum of all sales amounts.",
            }
        ],
        "visuals": [
            {
                "visual_type": "barChart",
                "table": "Sales",
                "measure": "Total Revenue",
                "dimension": "Region",
                "rationale": "Compare revenue across regions.",
            },
            {
                "visual_type": "card",
                "table": "Sales",
                "measure": "Total Revenue",
                "dimension": None,
                "rationale": "Show headline KPI.",
            },
        ],
    }
)


# ---------------------------------------------------------------------------
# SchemaAdvisor.advise
# ---------------------------------------------------------------------------


class TestSchemaAdvisorAdvise:
    def test_returns_parsed_result(self) -> None:
        advisor = SchemaAdvisor(_stub_llm(VALID_LLM_RESPONSE))
        result = advisor.advise(_build_profiles())
        assert result is not None
        # Three column semantics entries.
        assert len(result.column_semantics) == 3
        names = {(c.name, c.role) for c in result.column_semantics}
        assert ("Region", "dimension") in names
        assert ("Amount", "measure") in names
        assert ("Name", "dimension") in names
        # One measure suggestion.
        assert len(result.measure_suggestions) == 1
        m = result.measure_suggestions[0]
        assert m.name == "Total Revenue"
        assert m.expression == "SUM(Sales[Amount])"
        # Two visual suggestions.
        assert len(result.visual_suggestions) == 2
        assert result.visual_suggestions[0].visual_type == "barChart"

    def test_returns_none_when_llm_fails(self) -> None:
        class _Broken:
            def generate(self, messages):
                raise RuntimeError("network down")

        advisor = SchemaAdvisor(_Broken())
        result = advisor.advise(_build_profiles())
        assert result is None

    def test_returns_none_when_response_unparseable(self) -> None:
        advisor = SchemaAdvisor(_stub_llm("not even close to json"))
        result = advisor.advise(_build_profiles())
        assert result is None

    def test_tolerates_code_fenced_json(self) -> None:
        fenced = "```json\n" + VALID_LLM_RESPONSE + "\n```"
        advisor = SchemaAdvisor(_stub_llm(fenced))
        result = advisor.advise(_build_profiles())
        assert result is not None
        assert len(result.column_semantics) == 3

    def test_tolerates_leading_prose(self) -> None:
        wrapped = "Here is the analysis you asked for:\n\n" + VALID_LLM_RESPONSE
        advisor = SchemaAdvisor(_stub_llm(wrapped))
        result = advisor.advise(_build_profiles())
        assert result is not None
        assert len(result.column_semantics) == 3

    def test_empty_profiles_returns_none(self) -> None:
        # No data sources registered → empty profile list.
        advisor = SchemaAdvisor(_stub_llm(VALID_LLM_RESPONSE))
        result = advisor.advise([])
        assert result is None

    def test_invalid_role_normalised_to_unknown(self) -> None:
        # The LLM sometimes invents roles outside our enum. The
        # advisor normalises them to ``unknown`` rather than
        # crashing.
        response = json.dumps(
            {
                "columns": [
                    {
                        "table": "T",
                        "name": "x",
                        "role": "something_else",
                        "description": "weird role",
                        "suggested_measure_name": None,
                    }
                ],
                "measures": [],
                "visuals": [],
            }
        )
        advisor = SchemaAdvisor(_stub_llm(response))
        result = advisor.advise(_build_profiles())
        assert result is not None
        assert result.column_semantics[0].role == "unknown"

    def test_missing_required_fields_dropped(self) -> None:
        # An entry missing ``name`` or ``expression`` is dropped
        # entirely rather than crashing the whole parse.
        response = json.dumps(
            {
                "columns": [
                    {
                        "table": "T",
                        "name": "",
                        "role": "measure",
                        "description": "no name",
                        "suggested_measure_name": None,
                    }
                ],
                "measures": [
                    # Missing ``name`` — should be dropped.
                    {"table": "T", "expression": "SUM(T[x])"},
                    # Missing ``expression`` — should be dropped.
                    {"table": "T", "name": "X"},
                ],
                "visuals": [],
            }
        )
        advisor = SchemaAdvisor(_stub_llm(response))
        result = advisor.advise(_build_profiles())
        assert result is not None
        assert result.column_semantics == []  # empty name → skipped
        assert result.measure_suggestions == []  # both dropped

    def test_prompt_carries_profile_summary(self) -> None:
        """The LLM prompt contains the deterministic profile, not raw data."""

        captured: Dict[str, Any] = {}

        def fake_generate(messages):
            captured["messages"] = messages
            return VALID_LLM_RESPONSE

        class _LLM:
            generate = staticmethod(fake_generate)

        advisor = SchemaAdvisor(_LLM())
        advisor.advise(_build_profiles())
        # The user message is JSON; verify it carries table names
        # and column examples, but no raw row data.
        user_msg = json.loads(captured["messages"][1]["content"])
        tables = {t["name"] for t in user_msg["tables"]}
        assert "Sales" in tables
        assert "Region" in tables
        # Each column has distinct_count + examples — that's the
        # signal the LLM needs.
        sales_table = next(t for t in user_msg["tables"] if t["name"] == "Sales")
        amount_col = next(c for c in sales_table["columns"] if c["name"] == "Amount")
        # ``Amount`` is inferred as the broad ``numeric`` bucket by
        # the deterministic inspector — the LLM is expected to
        # specialise to ``decimal`` / ``currency`` in its semantic
        # analysis if appropriate.
        assert amount_col["inferred_type"] in {"numeric", "decimal", "currency"}
        assert amount_col["distinct_count"] == 200
        # Examples are top-5, not the full set.
        assert len(amount_col["examples"]) <= 5

    def test_cache_skips_second_llm_call(self) -> None:
        """Same profile shape → second call doesn't hit the LLM."""

        calls: List[int] = []

        def fake_generate(messages):
            calls.append(1)
            return VALID_LLM_RESPONSE

        # Wrap the bare function in a class so the advisor sees a
        # ``.generate`` method, matching the LLMClient protocol.
        class _LLM:
            generate = staticmethod(fake_generate)

        advisor = SchemaAdvisor(_LLM())
        advisor.advise(_build_profiles())
        advisor.advise(_build_profiles())  # cache hit
        assert len(calls) == 1

    def test_cache_disabled_when_size_zero(self) -> None:
        calls: List[int] = []

        def fake_generate(messages):
            calls.append(1)
            return VALID_LLM_RESPONSE

        class _LLM:
            generate = staticmethod(fake_generate)

        advisor = SchemaAdvisor(_LLM(), cache_size=0)
        advisor.advise(_build_profiles())
        advisor.advise(_build_profiles())
        # No cache → two LLM calls.
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorAISchemaHints:
    def _stub(self, reply: str = VALID_LLM_RESPONSE):
        return _stub_llm(reply)

    def test_ai_hints_included_when_data_sources_registered(
        self, tmp_path: Path
    ) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        ctx["data_sources"] = {
            "Sales": [
                {"Region": "EMEA", "Amount": 100.0},
                {"Region": "APAC", "Amount": 200.0},
            ]
        }
        orch = Orchestrator(llm_client=self._stub())
        payload = orch._planner_payload("test", ctx)
        assert "data_profile" in payload
        assert "ai_schema_hints" in payload
        hints = payload["ai_schema_hints"]
        # Column semantics parsed from the LLM response.
        assert "column_semantics" in hints
        assert "measure_suggestions" in hints
        assert "visual_suggestions" in hints

    def test_ai_hints_absent_when_no_data_sources(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        orch = Orchestrator(llm_client=self._stub())
        payload = orch._planner_payload("test", ctx)
        assert "ai_schema_hints" not in payload

    def test_ai_hints_omitted_when_disabled(self, tmp_path: Path) -> None:
        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "data_sources": {"Sales": [{"Region": "EMEA", "Amount": 100.0}]},
            # Explicit opt-out — useful when sending the planner
            # payload to a hosted LLM with strict data-residency.
            "data_inspector_ai_enabled": False,
        }
        orch = Orchestrator(llm_client=self._stub())
        payload = orch._planner_payload("test", ctx)
        assert "data_profile" in payload
        # AI hints are gated behind the opt-out flag.
        assert "ai_schema_hints" not in payload

    def test_ai_hints_fallback_when_llm_fails(self, tmp_path: Path) -> None:
        class _Broken:
            def generate(self, messages):
                raise RuntimeError("network down")

        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "data_sources": {"Sales": [{"Region": "EMEA", "Amount": 100.0}]},
        }
        orch = Orchestrator(llm_client=_Broken())
        payload = orch._planner_payload("test", ctx)
        # Deterministic profile still present even though the AI
        # advisor failed.
        assert "data_profile" in payload
        assert "ai_schema_hints" not in payload

    def test_ai_hints_fallback_when_response_unparseable(self, tmp_path: Path) -> None:
        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "data_sources": {"Sales": [{"Region": "EMEA", "Amount": 100.0}]},
        }
        orch = Orchestrator(llm_client=self._stub("garbage output"))
        payload = orch._planner_payload("test", ctx)
        assert "data_profile" in payload
        assert "ai_schema_hints" not in payload
