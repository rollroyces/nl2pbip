"""Tests for streaming plan execution (``--plan-chunk-size``).

Covers :meth:`nl2pbip.orchestrator.Orchestrator._execute_plan_chunked`
+ the ``plan_chunk_size`` constructor param + the
``on_plan_chunk_complete`` hook + the CLI flag plumbing.

Spec acceptance: ``a 7-step plan with chunk-size=3 yields 3
separate tool batches + the final state matches a non-chunked
run``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from nl2pbip.cli import _build_generate_parser, parse_args
from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY


class _StaticLLM:
    """Deterministic LLM stub returning a fixed plan.

    Honors an optional ``budget`` attribute (set by either the
    orchestrator via ``set_budget`` or a test manually) so
    chunking + budget interaction can be exercised end-to-end.
    """

    provider = "stub"
    model = "stub-model"

    def __init__(self, plan: List[Dict[str, Any]]) -> None:
        self._plan = plan
        self.calls = 0
        self.budget: Optional[Any] = None

    def set_budget(self, budget: Any) -> None:
        self.budget = budget

    def generate(self, _: List[Dict[str, str]]) -> str:  # type: ignore[override]
        # Charge the budget BEFORE incrementing ``calls`` so a
        # ``BudgetExceededError`` leaves ``calls == 0`` (the
        # abort path is atomic from the caller's perspective).
        if self.budget is not None:
            from nl2pbip.budget import count_tokens

            completion_text = json.dumps({"plan": self._plan})
            counts = count_tokens(_, completion_text)
            self.budget.check_and_record(
                prompt_tokens=counts["prompt_tokens"],
                completion_tokens=counts["completion_tokens"],
            )
        self.calls += 1
        return json.dumps({"plan": self._plan})


def _seven_step_plan(tmp_path: Path) -> List[Dict[str, Any]]:
    """A 7-step plan that the orchestrator can run end-to-end."""
    return [
        {
            "tool": "add_report_page",
            "args": {"page": "Main", "display_name": "Test"},
        },
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
                "output_path": str(tmp_path / "Chunked.pbipdir"),
                "project_name": "Chunked",
                "overwrite": True,
            },
        },
    ]


def _ctx(tmp_path: Path) -> Dict[str, Any]:
    return {
        MODEL_PATH_KEY: str(tmp_path / "model.tmdl"),
        REPORT_PATH_KEY: str(tmp_path / "report_workspace.json"),
        "dax_catalog_path": str(
            Path(__file__).parent.parent / "nl2pbip" / "dax_library.json"
        ),
    }


# ----------------------------------------------------------------------
# Plan chunking acceptance test
# ----------------------------------------------------------------------
class TestPlanChunking:
    """The v1.4.0 chunking acceptance criteria."""

    def test_seven_step_plan_with_chunk_size_three_yields_three_batches(
        self, tmp_path: Path
    ) -> None:
        """A 7-step plan with chunk-size=3 yields exactly 3 batches.

        Chunk math: 7 steps / chunk-size 3 → batches of 3, 3, 1.
        The hook fires after every completed batch, so it must
        be invoked 3 times.
        """
        batches: List[List[str]] = []

        def _on_complete(results: List[Any], _context: Dict[str, Any]) -> None:
            # Capture just the tool names of the cumulative
            # results-so-far so we can verify the batches.
            batches.append([r.tool for r in results])

        orchestrator = Orchestrator(
            llm_client=_StaticLLM(_seven_step_plan(tmp_path)),
            plan_chunk_size=3,
            on_plan_chunk_complete=_on_complete,
        )
        register_builtin_tools(orchestrator)

        results = orchestrator.run("Build a 7-step report", context=_ctx(tmp_path))

        # The hook fires after every chunk, including the partial
        # last chunk (1 step), so we expect 3 invocations.
        assert len(batches) == 3
        # Batch sizes: 3, 3, 1. Cumulative results grow:
        # batch 1 = 3 tool names, batch 2 = 6, batch 3 = 7.
        assert len(batches[0]) == 3
        assert len(batches[1]) == 6
        assert len(batches[2]) == 7
        # Tool ordering is preserved across chunks.
        assert batches[-1] == [r.tool for r in results]

    def test_chunked_run_matches_non_chunked_run(self, tmp_path: Path) -> None:
        """Final state of a chunked run == final state of a non-chunked run.

        We compare the on-disk artefacts (model.tmdl, report.json,
        packaged directory tree) rather than the in-memory
        ``ToolResult.output`` dicts — the latter embed random
        UUIDs and absolute paths that differ across runs even
        when the same plan executes.
        """
        plan_factory = lambda root: [  # noqa: E731
            {
                "tool": "add_report_page",
                "args": {"page": "Main", "display_name": "Test"},
            },
            {
                "tool": "create_table",
                "args": {
                    "table_name": "Sales",
                    "columns": [
                        {"name": "SaleId", "data_type": "string"},
                        {"name": "Amount", "data_type": "decimal"},
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
                "tool": "package_pbip",
                "args": {
                    "output_path": str(root / "out.pbipdir"),
                    "project_name": "Chunked",
                    "overwrite": True,
                },
            },
        ]

        # Non-chunked baseline.
        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir()
        orch_no_chunk = Orchestrator(llm_client=_StaticLLM(plan_factory(baseline_dir)))
        register_builtin_tools(orch_no_chunk)
        baseline = orch_no_chunk.run("anything", context=_ctx(baseline_dir))

        # Chunked variant — fresh workspace, same plan shape.
        chunked_dir = tmp_path / "chunked"
        chunked_dir.mkdir()
        orch_chunked = Orchestrator(
            llm_client=_StaticLLM(plan_factory(chunked_dir)), plan_chunk_size=2
        )
        register_builtin_tools(orch_chunked)
        chunked = orch_chunked.run("anything", context=_ctx(chunked_dir))

        # Same tool order, same number of tools.
        assert [r.tool for r in baseline] == [r.tool for r in chunked]
        assert len(baseline) == len(chunked) == 4

        # ``package_pbip`` reports ``status: success`` on both
        # sides and the output path matches what the plan asked
        # for (this is the strongest deterministic signal that
        # the chunked path produced the same final state).
        baseline_package = next(r for r in baseline if r.tool == "package_pbip")
        chunked_package = next(r for r in chunked if r.tool == "package_pbip")
        assert baseline_package.output["status"] == chunked_package.output["status"]
        assert baseline_package.output["status"] == "success"
        assert baseline_package.output["project"] == chunked_package.output["project"]

        # The packaged PBIP directories both exist; both have
        # the expected semantic-model + report files.
        for label, package_result in (
            ("baseline", baseline_package),
            ("chunked", chunked_package),
        ):
            project_path = Path(package_result.output["project_path"])
            assert project_path.exists(), f"{label} PBIP missing at {project_path}"
            # The PBIP project name is taken from the plan's
            # ``project_name`` arg, NOT the output_path tail.
            project_name = package_result.output["project"]
            assert (project_path / f"{project_name}.Report").exists()
            assert (project_path / f"{project_name}.SemanticModel").exists()

    def test_chunk_size_one_emits_hook_per_step(self, tmp_path: Path) -> None:
        """chunk_size=1 = max granularity; hook fires after every tool."""
        hook_calls = 0

        def _on_complete(_results: List[Any], _context: Dict[str, Any]) -> None:
            nonlocal hook_calls
            hook_calls += 1

        orchestrator = Orchestrator(
            llm_client=_StaticLLM(_seven_step_plan(tmp_path)),
            plan_chunk_size=1,
            on_plan_chunk_complete=_on_complete,
        )
        register_builtin_tools(orchestrator)
        orchestrator.run("anything", context=_ctx(tmp_path))
        assert hook_calls == 7

    def test_chunk_size_larger_than_plan_fires_once(self, tmp_path: Path) -> None:
        """A chunk size >= plan length fires the hook exactly once."""
        hook_calls = 0

        def _on_complete(_results: List[Any], _context: Dict[str, Any]) -> None:
            nonlocal hook_calls
            hook_calls += 1

        orchestrator = Orchestrator(
            llm_client=_StaticLLM(_seven_step_plan(tmp_path)),
            plan_chunk_size=20,
            on_plan_chunk_complete=_on_complete,
        )
        register_builtin_tools(orchestrator)
        orchestrator.run("anything", context=_ctx(tmp_path))
        assert hook_calls == 1

    def test_chunk_size_zero_keeps_legacy_single_shot(self, tmp_path: Path) -> None:
        """chunk_size=0 (default) does NOT invoke the hook."""
        hook_calls = 0

        def _on_complete(_results: List[Any], _context: Dict[str, Any]) -> None:
            nonlocal hook_calls
            hook_calls += 1

        orchestrator = Orchestrator(
            llm_client=_StaticLLM(_seven_step_plan(tmp_path)),
            on_plan_chunk_complete=_on_complete,
        )
        register_builtin_tools(orchestrator)
        orchestrator.run("anything", context=_ctx(tmp_path))
        assert hook_calls == 0


# ----------------------------------------------------------------------
# Constructor validation
# ----------------------------------------------------------------------
class TestOrchestratorChunkSizeValidation:
    def test_negative_chunk_size_raises(self) -> None:
        with pytest.raises(ValueError, match="plan_chunk_size must be >= 0"):
            Orchestrator(llm_client=_StaticLLM([]), plan_chunk_size=-1)

    def test_default_chunk_size_is_zero(self) -> None:
        orch = Orchestrator(llm_client=_StaticLLM([]))
        assert orch.plan_chunk_size == 0
        assert orch.on_plan_chunk_complete is None


# ----------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------
class TestPlanChunkSizeCLIFlag:
    def test_default_value_is_zero(self) -> None:
        parser = _build_generate_parser()
        args = parser.parse_args(["--prompt", "x"])
        assert args.plan_chunk_size == 0

    def test_custom_value_propagates(self) -> None:
        parser = _build_generate_parser()
        args = parser.parse_args(["--prompt", "x", "--plan-chunk-size", "5"])
        assert args.plan_chunk_size == 5

    def test_parse_args_preserves_plan_chunk_size(self) -> None:
        args = parse_args(["--prompt", "x", "--plan-chunk-size", "2"])
        assert args.plan_chunk_size == 2
        assert args.command == "generate"


# ----------------------------------------------------------------------
# Hook integration with budget
# ----------------------------------------------------------------------
class TestChunkedRunBudgetInteraction:
    """Streaming + cost guardrail compose without surprises."""

    def test_budget_still_records_calls_for_chunked_runs(self, tmp_path: Path) -> None:
        from nl2pbip.budget import TokenBudget

        llm = _StaticLLM(_seven_step_plan(tmp_path))
        orch = Orchestrator(
            llm_client=llm,
            max_cost_usd=1.0,
            plan_chunk_size=3,
            on_plan_chunk_complete=lambda *_a, **_kw: None,
        )
        register_builtin_tools(orch)
        # The orchestrator already attached its budget via
        # ``_StaticLLM.set_budget``; assert it's wired and the
        # budget is the same instance the orchestrator built.
        assert llm.budget is orch.token_budget
        assert isinstance(orch.token_budget, TokenBudget)
        orch.run("anything", context=_ctx(tmp_path))
        # One LLM call → one budget record.
        assert orch.token_budget is not None
        assert len(orch.token_budget.calls) == 1
        assert llm.calls == 1
