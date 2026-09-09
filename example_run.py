"""Example end-to-end execution that produces a PBIP folder."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from orchestrator import Orchestrator, register_builtin_tools
from pbir_engine import REPORT_PATH_KEY
from tmdl_engine import MODEL_PATH_KEY


class StaticPlanLLM:
    """Deterministic LLM stub that always returns the provided plan."""

    def __init__(self, plan: List[Dict[str, Any]]) -> None:
        self._plan = plan

    def generate(self, _: List[Dict[str, str]]) -> str:  # type: ignore[override]
        return json.dumps(self._plan, indent=2)


def build_sample_plan(project_dir: Path) -> List[Dict[str, Any]]:
    return [
        {
            "tool": "add_report_page",
            "args": {"page": "Main", "display_name": "Executive Overview"},
        },
        {
            "tool": "create_table",
            "args": {
                "table_name": "Date",
                "columns": [
                    {"name": "Date", "data_type": "date"},
                    {"name": "Month", "data_type": "string"},
                    {"name": "Year", "data_type": "wholeNumber"},
                ],
            },
        },
        {
            "tool": "create_table",
            "args": {
                "table_name": "Sales",
                "columns": [
                    {"name": "SaleId", "data_type": "string"},
                    {"name": "Region", "data_type": "string"},
                    {"name": "Amount", "data_type": "decimal"},
                    {"name": "Date", "data_type": "date"},
                ],
            },
        },
        {
            "tool": "add_measure",
            "args": {
                "table_name": "Sales",
                "measure_name": "Total Revenue",
                "expression": "SUM(Sales[Amount])",
                "format_string": "$#,0.00",
            },
        },
        {
            "tool": "define_relationship",
            "args": {
                "from_table": "Sales",
                "from_column": "Date",
                "to_table": "Date",
                "to_column": "Date",
                "cardinality": "manyToOne",
                "cross_filter_direction": "single",
            },
        },
        {
            "tool": "add_visual",
            "args": {
                "page": "Main",
                "visual_type": "columnChart",
                "bindings": {
                    "Category": ["Sales[Region]"],
                    "Values": ["Sales[Total Revenue]"],
                },
                "title": "Revenue by Region",
                "position": {"width": 520, "height": 300},
            },
        },
        {
            "tool": "package_pbip",
            "args": {
                "output_path": str(project_dir / "SalesInsights.pbipdir"),
                "project_name": "SalesInsights",
                "overwrite": True,
            },
        },
    ]


def main() -> None:
    workspace = Path(__file__).parent
    artifact_dir = workspace / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    model_path = artifact_dir / "semantic_workspace" / "model.tmdl"
    report_path = artifact_dir / "report_workspace.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    plan = build_sample_plan(artifact_dir)
    orchestrator = Orchestrator(llm_client=StaticPlanLLM(plan))
    register_builtin_tools(orchestrator)

    context = {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
    }

    prompt = "Create a sales insights dashboard with date intelligence and region breakdowns."
    results = orchestrator.run(prompt, context=context)

    print("Executed plan containing", len(results), "steps.")
    for result in results:
        print(f"- {result.tool}: {result.output}")

    print("PBIP output located at:", plan[-1]["args"]["output_path"])


if __name__ == "__main__":
    main()
