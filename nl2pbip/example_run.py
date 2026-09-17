"""Example end-to-end execution that produces a PBIP folder."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY

# Module-level logger — emits at INFO by default; use
# ``--log-level`` on the CLI to override. Captured by the root
# handler so the smoke test in
# ``tests/test_repo_templates.py::TestRepoTemplateIntegration::
# test_example_run_produces_fabric_metadata`` continues to see
# output (it asserts on the artifact files, not on stdout).
logger = logging.getLogger(__name__)


class StaticPlanLLM:
    """Deterministic LLM stub that always returns the provided plan."""

    def __init__(self, plan: List[Dict[str, Any]]) -> None:
        self._plan = plan

    def generate(self, _: List[Dict[str, str]]) -> str:
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
            "tool": "add_power_query_partition",
            "args": {
                "table_name": "Sales",
                "template": "csv",
                "params": {"path": "raw/sales.csv"},
                "promote": True,
                "column_types": [
                    {"name": "SaleId", "type": "text"},
                    {"name": "Region", "type": "text"},
                    {"name": "Amount", "type": "number"},
                    {"name": "Date", "type": "date"},
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
            "tool": "add_calculation_group",
            "args": {
                "table_name": "CG Time Intelligence",
                "precedence": 10,
                "items": [
                    {"name": "Current", "expression": "SELECTEDMEASURE()"},
                    {
                        "name": "YTD",
                        "expression": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))",
                    },
                    {
                        "name": "Prior Year",
                        "expression": "CALCULATE(SELECTEDMEASURE(), DATEADD('Date'[Date], -1, YEAR))",
                    },
                    {
                        "name": "YoY %",
                        "expression": "DIVIDE(SELECTEDMEASURE() - CALCULATE(SELECTEDMEASURE(), DATEADD('Date'[Date], -1, YEAR)), CALCULATE(SELECTEDMEASURE(), DATEADD('Date'[Date], -1, YEAR)))",
                        "format_string": "0.00%",
                        "format_string_definition": 'VAR x = SELECTEDMEASUREFORMATSTRING() RETURN IF(ISNUMERIC(x), "0.00%", x)',
                    },
                ],
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
            "tool": "add_field_parameter",
            "args": {
                "parameter_name": "Metric Selection",
                "members": [
                    {
                        "display_name": "Total Revenue",
                        "table_name": "Sales",
                        "measure_name": "Total Revenue",
                    },
                    {
                        "display_name": "Region",
                        "table_name": "Sales",
                        "column_name": "Region",
                    },
                ],
            },
        },
        {
            "tool": "add_ols_role",
            "args": {
                "role_name": "SalesPublic",
                "model_permission": "read",
                "column_permissions": [
                    {
                        "table_name": "Sales",
                        "column_name": "SaleId",
                        "metadata_permission": "none",
                    },
                ],
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


def main(argv: Optional[List[str]] = None) -> None:
    """Run the example plan end-to-end.

    Accepts an optional ``argv`` list so the function can be
    unit-tested without mutating ``sys.argv``. The default
    ``None`` reads ``sys.argv[1:]`` for parity with the CLI
    convention used elsewhere in the package.

    Recognised flags:

    * ``--log-level LEVEL`` — one of DEBUG / INFO / WARNING /
      ERROR / CRITICAL (case-insensitive). Defaults to INFO.
      The level applies to the ``nl2pbip.example_run``
      logger; the root logger keeps WARNING so unrelated
      library noise doesn't leak into the demo output.
    """
    parser = argparse.ArgumentParser(
        prog="python -m nl2pbip.example_run",
        description="End-to-end demo: builds a PBIP folder from a "
        "static plan (mock LLM).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level for the demo logger (default: INFO).",
    )
    parsed = parser.parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, parsed.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(getattr(logging, parsed.log_level.upper(), logging.INFO))

    # Resolve the project root regardless of whether example_run.py is
    # imported as a script or as ``python -m nl2pbip.example_run``.
    # ``Path(__file__).parent`` is the package directory; the example
    # artifacts live at the project root.
    package_dir = Path(__file__).resolve().parent
    workspace = package_dir.parent if package_dir.name == "nl2pbip" else package_dir
    artifact_dir = workspace / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    model_path = artifact_dir / "semantic_workspace" / "model.tmdl"
    report_path = artifact_dir / "report_workspace.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Idempotency: clear stale workspace state from prior runs so the
    # orchestrator starts from a clean slate (otherwise add_report_page
    # raises "Page 'Main' already exists." on the second run).
    if model_path.exists():
        model_path.unlink()
    if report_path.exists():
        report_path.unlink()
    existing_package = artifact_dir / "SalesInsights.pbipdir"
    if existing_package.exists():
        import shutil

        shutil.rmtree(existing_package)

    plan = build_sample_plan(artifact_dir)
    orchestrator = Orchestrator(llm_client=StaticPlanLLM(plan))
    register_builtin_tools(orchestrator)

    context = {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
    }

    prompt = "Create a sales insights dashboard with date intelligence and region breakdowns."
    results = orchestrator.run(prompt, context=context)

    logger.info("Executed plan containing %d steps.", len(results))
    for result in results:
        logger.info("- %s: %s", result.tool, result.output)

    logger.info("PBIP output located at: %s", plan[-1]["args"]["output_path"])


if __name__ == "__main__":
    main()
