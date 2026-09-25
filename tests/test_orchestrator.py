"""Smoke tests for the orchestrator's plan/execute/retry loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY


class _StaticLLM:
    """Deterministic LLM stub. Returns ``plan`` on every call."""

    provider = "stub"
    model = "stub-model"

    def __init__(self, plan: List[Dict[str, Any]]) -> None:
        self._plan = plan
        self.calls = 0

    def generate(self, _: List[Dict[str, str]]) -> str:  # type: ignore[override]
        self.calls += 1
        return json.dumps({"plan": self._plan})


def _sample_plan(tmp_path: Path) -> List[Dict[str, Any]]:
    return [
        {"tool": "add_report_page", "args": {"page": "Main", "display_name": "Test"}},
        {
            "tool": "create_table",
            "args": {
                "table_name": "Date",
                "columns": [{"name": "Date", "data_type": "date"}],
            },
        },
        {
            "tool": "create_table",
            "args": {
                "table_name": "Sales",
                "columns": [
                    {"name": "SaleId", "data_type": "string"},
                    {"name": "Amount", "data_type": "decimal"},
                    {"name": "Date", "data_type": "date"},
                ],
            },
        },
        {
            "tool": "add_measure",
            "args": {
                "table_name": "Sales",
                "measure_name": "Revenue",
                "expression": "SUM(Sales[Amount])",
            },
        },
        {
            "tool": "define_relationship",
            "args": {
                "from_table": "Sales",
                "from_column": "Date",
                "to_table": "Date",
                "to_column": "Date",
            },
        },
        {
            "tool": "add_visual",
            "args": {
                "page": "Main",
                "visual_type": "columnChart",
                "bindings": {
                    "Category": ["Sales[SaleId]"],
                    "Values": ["[Revenue]"],
                },
            },
        },
        {
            "tool": "package_pbip",
            "args": {
                "output_path": str(tmp_path / "Sample.pbipdir"),
                "project_name": "Sample",
                "overwrite": True,
            },
        },
    ]


def test_orchestrator_runs_full_plan(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    report_path = tmp_path / "report_workspace.json"

    orchestrator = Orchestrator(llm_client=_StaticLLM(_sample_plan(tmp_path)))
    register_builtin_tools(orchestrator)

    results = orchestrator.run(
        "Build a sample report",
        context={
            MODEL_PATH_KEY: str(model_path),
            REPORT_PATH_KEY: str(report_path),
            "dax_catalog_path": str(
                Path(__file__).parent.parent / "nl2pbip" / "dax_library.json"
            ),
        },
    )

    assert [r.tool for r in results] == [
        "add_report_page",
        "create_table",
        "create_table",
        "add_measure",
        "define_relationship",
        "add_visual",
        "package_pbip",
    ]
    # All tool calls succeeded
    for result in results:
        assert result.output["status"] == "success"
    # Final package_pbip output has the expected structure
    package = next(r for r in results if r.tool == "package_pbip")
    assert package.output["project"] == "Sample"
    assert Path(package.output["project_path"]).exists()


def test_orchestrator_rejects_non_json_plan() -> None:
    class _BadLLM:
        provider = "stub"
        model = "stub-model"

        def generate(self, _messages: List[Dict[str, str]]) -> str:
            return "not json at all"

    orchestrator = Orchestrator(llm_client=_BadLLM())
    register_builtin_tools(orchestrator)

    with pytest.raises(ValueError, match="Planner must return valid JSON"):
        orchestrator.run("anything")


def test_orchestrator_rejects_unknown_tool() -> None:
    orchestrator = Orchestrator(
        llm_client=_StaticLLM([{"tool": "definitely_not_a_real_tool", "args": {}}])
    )
    register_builtin_tools(orchestrator)
    with pytest.raises(ValueError, match="Unknown tool"):
        orchestrator.run(
            "anything",
            context={"model_path": "/tmp/x.tmdl", "report_path": "/tmp/x.json"},
        )


def test_orchestrator_validates_required_payload_fields(tmp_path: Path) -> None:
    # Missing 'columns' for create_table
    orchestrator = Orchestrator(
        llm_client=_StaticLLM([{"tool": "create_table", "args": {"table_name": "X"}}])
    )
    register_builtin_tools(orchestrator)
    with pytest.raises(ValueError, match="missing required"):
        orchestrator.run(
            "anything",
            context={
                "model_path": str(tmp_path / "m.tmdl"),
                "report_path": str(tmp_path / "r.json"),
            },
        )


# ---------------------------------------------------------------------------
# Lightweight RAG for model_state (v1.3.6)
# ---------------------------------------------------------------------------
from nl2pbip.tmdl_engine import (  # noqa: E402  -- grouped below for clarity
    TMDLColumn,
    TMDLModel,
    TMDLRelationship,
    TMDLTable,
    _render_model_body,
)


def _build_multi_table_model(tmp_path: Path) -> Path:
    """Build a TMDL model file with several unrelated tables + one shared dim.

    The orchestrator's RAG filter must keep only the table(s) the
    caller is focusing on (via ``data_sources`` keys / lint hints)
    and drop the rest, plus emit a content-hash so the LLM can
    verify the unseen tail hasn't drifted.
    """
    model_path = tmp_path / "model.tmdl"
    model = TMDLModel()
    dim = TMDLTable(name="Date")
    for col_name, dt in [("Date", "date"), ("Year", "wholeNumber")]:
        dim.add_column(TMDLColumn(name=col_name, data_type=dt))
    model.add_table(dim)
    for table_name, columns in [
        ("Sales", ["SaleId", "Amount", "Region"]),
        ("Inventory", ["Sku", "OnHand"]),
        ("Returns", ["ReturnId", "Reason"]),
        ("Customers", ["CustomerId", "Email"]),
        ("Suppliers", ["SupplierId", "Country"]),
        ("AuditLog", ["EventId", "Actor"]),
    ]:
        t = TMDLTable(name=table_name)
        for col_name in columns:
            t.add_column(TMDLColumn(name=col_name, data_type="string"))
        model.add_table(t)
    model.add_relationship(
        TMDLRelationship(
            name="Sales_Date",
            from_table="Sales",
            from_column="Date",
            to_table="Date",
            to_column="Date",
            cardinality="manyToOne",
        )
    )
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return model_path


class TestModelStateRAG:
    """Regression tests for the model_state RAG filter (Phase 2 / Item 1)."""

    def test_full_model_state_emitted_when_no_hints(self, tmp_path: Path) -> None:
        """No focus hints → emit the full summary (back-compat)."""
        model_path = _build_multi_table_model(tmp_path)
        orch = Orchestrator(llm_client=_StaticLLM([]))
        summary = orch._summarise_model({MODEL_PATH_KEY: str(model_path)})
        assert summary is not None
        assert summary["table_count"] == 7  # Date + 6 facts
        assert set(summary["tables"].keys()) == {
            "Date",
            "Sales",
            "Inventory",
            "Returns",
            "Customers",
            "Suppliers",
            "AuditLog",
        }
        # No RAG metadata when un-filtered.
        assert "content_hash" not in summary
        assert summary.get("rag_filtered") is not True

    def test_filtered_summary_shrinks_with_data_source_hints(
        self, tmp_path: Path
    ) -> None:
        """Tiny data_source focus → summary only contains that slice + hash."""
        model_path = _build_multi_table_model(tmp_path)
        full_size = len(
            json.dumps(
                Orchestrator(llm_client=_StaticLLM([]))._summarise_model(
                    {MODEL_PATH_KEY: str(model_path)}
                )
            )
        )
        ctx = {
            MODEL_PATH_KEY: str(model_path),
            "data_sources": {
                # Only one focus table — RAG should drop the other 5
                # + the Date dimension (since it's not in the hint).
                "Sales": [{"SaleId": "S1", "Amount": 10.0}],
            },
        }
        orch = Orchestrator(llm_client=_StaticLLM([]))
        summary = orch._summarise_model(ctx)
        assert summary is not None
        # The RAG slice must be smaller than the full payload.
        filtered_size = len(json.dumps(summary))
        assert filtered_size < full_size, (
            f"RAG filter didn't shrink payload: full={full_size} "
            f"filtered={filtered_size}"
        )
        # Hash + filter markers must be present.
        assert summary.get("rag_filtered") is True
        assert isinstance(summary.get("content_hash"), str)
        assert len(summary["content_hash"]) >= 8
        # The focus table is included.
        assert "Sales" in summary["tables"]
        # Unrelated tables are dropped.
        assert "Inventory" not in summary["tables"]
        assert "Returns" not in summary["tables"]
        assert "Suppliers" not in summary["tables"]
        # Total table_count reflects the full model (so the LLM
        # knows there's more than it's seeing).
        assert summary["table_count"] == 7

    def test_lint_error_hints_keep_referenced_tables(self, tmp_path: Path) -> None:
        """Recent lint errors mentioning Table[Col] keep those tables."""
        model_path = _build_multi_table_model(tmp_path)
        ctx = {
            MODEL_PATH_KEY: str(model_path),
            "recent_lint_errors": [
                "Undefined column Customers[Email] referenced by measure",
            ],
        }
        orch = Orchestrator(llm_client=_StaticLLM([]))
        summary = orch._summarise_model(ctx)
        assert summary is not None
        assert summary.get("rag_filtered") is True
        assert "Customers" in summary["tables"]

    def test_rag_disabled_via_context(self, tmp_path: Path) -> None:
        """``model_state_rag_enabled = False`` skips the filter."""
        model_path = _build_multi_table_model(tmp_path)
        ctx = {
            MODEL_PATH_KEY: str(model_path),
            "data_sources": {"Sales": [{"SaleId": "S1"}]},
            "model_state_rag_enabled": False,
        }
        orch = Orchestrator(llm_client=_StaticLLM([]))
        summary = orch._summarise_model(ctx)
        assert summary is not None
        assert "content_hash" not in summary
        assert summary.get("rag_filtered") is not True
        assert summary["table_count"] == 7
