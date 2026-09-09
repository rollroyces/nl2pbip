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
