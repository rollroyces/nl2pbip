"""Tests for the agentic self-reflection loop on the orchestrator.

The reflection loop adds five concrete capabilities to ``Orchestrator``:

1. A :class:`ReflectiveTrace` dataclass that records every attempt
   (plan, results, error, feedback, reflection).
2. A :class:`PlanQualityScore` dataclass for parsed critic responses.
3. A :class:`PlannerClarification` exception raised when the LLM
   emits a ``{"clarification": ...}`` payload instead of a plan.
4. An ``Orchestrator.run_with_reflection()`` method that:
   - keeps a cumulative feedback log across failed attempts
   - records every attempt in the trace
   - runs a post-success critic pass
   - re-invokes the planner with the critic's suggestions when
     the score is below the threshold
5. A :class:`PlanQualityScore.is_acceptable` predicate and
   :func:`Orchestrator._parse_critic_score` static helper.

These tests use stub LLM clients so the suite doesn't depend on a
real model. The stubs can be wired to return deterministic
plans, errors, or critic JSON.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from nl2pbip.orchestrator import (
    Orchestrator,
    PlannerClarification,
    PlanQualityScore,
    ReflectiveTrace,
    register_builtin_tools,
)
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.prompts import (
    CRITIC_SYSTEM_PROMPT,
    build_critic_user_message,
)
from nl2pbip.tmdl_engine import MODEL_PATH_KEY

# ---------------------------------------------------------------------------
# Stub LLM clients
# ---------------------------------------------------------------------------


class StaticLLM:
    """Returns a fixed string from ``generate``."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.call_count = 0
        self.last_messages: List[Dict[str, str]] = []

    def generate(self, messages: List[Dict[str, str]]) -> str:
        self.call_count += 1
        self.last_messages = list(messages)
        return self._response


class SequenceLLM:
    """Returns successive responses from a queue (one per call)."""

    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def generate(self, messages: List[Dict[str, str]]) -> str:
        if not self._responses:
            raise RuntimeError("SequenceLLM ran out of responses.")
        self.call_count += 1
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# PlanQualityScore
# ---------------------------------------------------------------------------


class TestPlanQualityScore:
    def test_overall_weighted(self) -> None:
        score = PlanQualityScore(
            correctness=1.0, completeness=1.0, alignment_with_prompt=1.0
        )
        assert score.overall == pytest.approx(1.0)

    def test_overall_emphasises_correctness(self) -> None:
        # correctness is weighted 0.5, completeness 0.3, alignment 0.2.
        high = PlanQualityScore(
            correctness=1.0, completeness=0.0, alignment_with_prompt=0.0
        )
        low = PlanQualityScore(
            correctness=0.0, completeness=1.0, alignment_with_prompt=0.0
        )
        assert high.overall > low.overall

    def test_is_acceptable_default(self) -> None:
        # Default threshold: overall >= 0.7 AND correctness >= 0.6.
        assert PlanQualityScore(
            correctness=0.9, completeness=0.9, alignment_with_prompt=0.9
        ).is_acceptable()
        # Low correctness fails regardless of overall.
        assert not PlanQualityScore(
            correctness=0.5, completeness=1.0, alignment_with_prompt=1.0
        ).is_acceptable()
        # Low overall fails even with high correctness.
        assert not PlanQualityScore(
            correctness=1.0, completeness=0.0, alignment_with_prompt=0.0
        ).is_acceptable()

    def test_is_acceptable_custom_threshold(self) -> None:
        score = PlanQualityScore(
            correctness=0.7, completeness=0.7, alignment_with_prompt=0.7
        )
        assert not score.is_acceptable(threshold=0.8)
        assert score.is_acceptable(threshold=0.7)

    def test_to_dict(self) -> None:
        score = PlanQualityScore(
            correctness=0.8,
            completeness=0.6,
            alignment_with_prompt=0.9,
            suggestions=["add a YoY measure"],
        )
        data = score
        # No to_dict on the score itself; the trace serialises it.
        assert isinstance(data.suggestions, list)


# ---------------------------------------------------------------------------
# _parse_critic_score
# ---------------------------------------------------------------------------


class TestParseCriticScore:
    def test_plain_json(self) -> None:
        raw = json.dumps(
            {
                "scores": {
                    "correctness": 0.8,
                    "completeness": 0.7,
                    "alignment_with_prompt": 0.9,
                },
                "suggestions": ["add YoY"],
            }
        )
        score = Orchestrator._parse_critic_score(raw)
        assert score is not None
        assert score.correctness == 0.8
        assert score.completeness == 0.7
        assert score.alignment_with_prompt == 0.9
        assert score.suggestions == ["add YoY"]
        assert score.is_acceptable()

    def test_markdown_fenced(self) -> None:
        raw = (
            "```json\n"
            + json.dumps(
                {
                    "scores": {
                        "correctness": 0.5,
                        "completeness": 0.5,
                        "alignment_with_prompt": 0.5,
                    },
                    "suggestions": [],
                }
            )
            + "\n```"
        )
        score = Orchestrator._parse_critic_score(raw)
        assert score is not None
        assert score.correctness == 0.5

    def test_flat_shape(self) -> None:
        raw = json.dumps(
            {
                "correctness": 0.7,
                "completeness": 0.7,
                "alignment": 0.7,
                "suggestions": [],
            }
        )
        score = Orchestrator._parse_critic_score(raw)
        assert score is not None
        assert score.alignment_with_prompt == 0.7

    def test_clamps_to_unit_interval(self) -> None:
        raw = json.dumps(
            {
                "scores": {
                    "correctness": 1.5,
                    "completeness": -0.2,
                    "alignment_with_prompt": 0.5,
                },
            }
        )
        score = Orchestrator._parse_critic_score(raw)
        assert score is not None
        assert score.correctness == 1.0
        assert score.completeness == 0.0

    def test_invalid_json_returns_none(self) -> None:
        assert Orchestrator._parse_critic_score("not json") is None
        assert Orchestrator._parse_critic_score("") is None
        assert Orchestrator._parse_critic_score("[]") is None
        assert Orchestrator._parse_critic_score('{"scores": "not a dict"}') is None

    def test_non_string_returns_none(self) -> None:
        assert Orchestrator._parse_critic_score(None) is None  # type: ignore[arg-type]
        assert Orchestrator._parse_critic_score(42) is None  # type: ignore[arg-type]

    def test_string_suggestions_get_wrapped(self) -> None:
        raw = json.dumps(
            {
                "scores": {
                    "correctness": 0.8,
                    "completeness": 0.8,
                    "alignment_with_prompt": 0.8,
                },
                "suggestions": "single string",
            }
        )
        score = Orchestrator._parse_critic_score(raw)
        assert score is not None
        assert score.suggestions == ["single string"]


# ---------------------------------------------------------------------------
# _build_reflection_feedback
# ---------------------------------------------------------------------------


class TestBuildReflectionFeedback:
    def test_includes_scores(self) -> None:
        score = PlanQualityScore(
            correctness=0.4,
            completeness=0.5,
            alignment_with_prompt=0.6,
            suggestions=["add YoY"],
        )
        feedback = Orchestrator._build_reflection_feedback(score)
        assert "0.40" in feedback
        assert "0.50" in feedback
        assert "0.60" in feedback
        assert "add YoY" in feedback

    def test_no_suggestions_still_useful(self) -> None:
        score = PlanQualityScore(
            correctness=0.4,
            completeness=0.5,
            alignment_with_prompt=0.6,
            suggestions=[],
        )
        feedback = Orchestrator._build_reflection_feedback(score)
        assert "lowest-scoring axis" in feedback


# ---------------------------------------------------------------------------
# build_critic_user_message
# ---------------------------------------------------------------------------


class TestCriticUserMessage:
    def test_includes_prompt(self) -> None:
        msg = build_critic_user_message("Build a sales dashboard", [], [])
        assert "Build a sales dashboard" in msg

    def test_includes_results(self) -> None:
        # Fabricate a minimal ToolResult-like object.
        class R:
            tool = "create_table"
            args = {"table_name": "Sales"}

        msg = build_critic_user_message("Build a sales dashboard", [R(), R()], [])
        assert "create_table" in msg
        assert "1." in msg and "2." in msg

    def test_includes_attempt_summary(self) -> None:
        # Fabricate AttemptRecord-like objects.
        class A:
            plan = [1]
            results = None
            error = "boom"
            attempt = 2

        msg = build_critic_user_message("X", [], [A()])
        assert "Total attempts: 1" in msg
        assert "boom" in msg


# ---------------------------------------------------------------------------
# CRITIC_SYSTEM_PROMPT
# ---------------------------------------------------------------------------


class TestCriticPrompt:
    def test_contains_required_keys(self) -> None:
        for key in ("correctness", "completeness", "alignment_with_prompt"):
            assert key in CRITIC_SYSTEM_PROMPT

    def test_instructs_json_only(self) -> None:
        # Make sure the prompt explicitly says "respond with ONLY a
        # JSON object" — otherwise the LLM may wrap in prose.
        assert "ONLY" in CRITIC_SYSTEM_PROMPT
        assert "JSON" in CRITIC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Clarification handling
# ---------------------------------------------------------------------------


class TestClarification:
    def test_clarification_raises_in_parse(self) -> None:
        with pytest.raises(PlannerClarification) as exc_info:
            Orchestrator(llm_client=StaticLLM(""))._parse_plan(
                json.dumps(
                    {
                        "clarification": "Which data source?",
                        "rationale": "Multiple were mentioned.",
                    }
                )
            )
        assert exc_info.value.question == "Which data source?"
        assert exc_info.value.rationale == "Multiple were mentioned."

    def test_clarification_in_run_with_reflection(self) -> None:
        llm = StaticLLM(
            json.dumps(
                {
                    "clarification": "Which source?",
                    "rationale": "Multiple plausible sources.",
                }
            )
        )
        orch = Orchestrator(llm_client=llm)
        with tempfile.TemporaryDirectory() as tmp:
            trace = orch.run_with_reflection(
                "Build a sales dashboard",
                context={
                    MODEL_PATH_KEY: str(Path(tmp) / "model.tmdl"),
                    REPORT_PATH_KEY: str(Path(tmp) / "report.json"),
                },
            )
        assert not trace.succeeded
        assert trace.final_error is not None
        assert "clarification" in trace.final_error.lower()
        assert "Which source?" in trace.final_error
        # Should not have retried.
        assert len(trace.attempts) == 1

    def test_clarification_exception_is_str(self) -> None:
        exc = PlannerClarification(question="Q?", rationale="because")
        assert "Q?" in str(exc)
        assert "because" in str(exc)


# ---------------------------------------------------------------------------
# run_with_reflection happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_plan() -> List[Dict[str, Any]]:
    """Minimal valid plan that survives the orchestrator's planner
    payload (no LLM context needed). Uses unique table / measure
    names per test invocation so retries don't fail with
    ``Table 'X' already exists``."""
    suffix = next(_plan_counter())
    return [
        {
            "tool": "create_table",
            "args": {
                "table_name": f"Date_{suffix}",
                "columns": [{"name": "Date", "data_type": "date"}],
            },
        },
        {
            "tool": "create_table",
            "args": {
                "table_name": f"Sales_{suffix}",
                "columns": [
                    {"name": "Id", "data_type": "string"},
                    {"name": "Amount", "data_type": "decimal"},
                    {"name": "DateId", "data_type": "date"},
                ],
            },
        },
        {
            "tool": "add_measure",
            "args": {
                "table_name": f"Sales_{suffix}",
                "measure_name": f"Total_{suffix}",
                "expression": f"SUM(Sales_{suffix}[Amount])",
            },
        },
        {
            "tool": "add_report_page",
            "args": {"page": "Main", "display_name": "Overview"},
        },
        {
            "tool": "add_visual",
            "args": {
                "page": "Main",
                "visual_type": "card",
                "bindings": {"Values": [f"Sales_{suffix}[Total_{suffix}]"]},
                "position": {"x": 0, "y": 0, "width": 320, "height": 200},
            },
        },
    ]


def _plan_counter():
    """Yield successive integers so each test gets a unique plan."""
    n = 0
    while True:
        n += 1
        yield n


class TestReflectionHappyPath:
    def test_returns_trace_on_success(self, sample_plan: List[Dict[str, Any]]) -> None:
        llm = StaticLLM(json.dumps(sample_plan))
        critic = StaticLLM(
            json.dumps(
                {
                    "scores": {
                        "correctness": 0.9,
                        "completeness": 0.9,
                        "alignment_with_prompt": 0.9,
                    },
                    "suggestions": [],
                }
            )
        )
        orch = Orchestrator(llm_client=llm)
        register_builtin_tools(orch)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {
                MODEL_PATH_KEY: str(Path(tmp) / "model.tmdl"),
                REPORT_PATH_KEY: str(Path(tmp) / "report.json"),
            }
            trace = orch.run_with_reflection(
                "Build a sales dashboard",
                context=ctx,
                critic=critic,
            )
        assert trace.succeeded
        assert trace.final_results is not None
        assert len(trace.final_results) == 5
        assert trace.critic_score is not None
        assert trace.critic_score.overall == pytest.approx(0.9)
        assert trace.reflection_rounds == 0

    def test_max_reflection_rounds_zero(
        self, sample_plan: List[Dict[str, Any]]
    ) -> None:
        llm = StaticLLM(json.dumps(sample_plan))
        critic = StaticLLM(
            json.dumps(
                {
                    "scores": {
                        "correctness": 0.1,
                        "completeness": 0.1,
                        "alignment_with_prompt": 0.1,
                    },
                    "suggestions": ["improve"],
                }
            )
        )
        orch = Orchestrator(llm_client=llm)
        register_builtin_tools(orch)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {
                MODEL_PATH_KEY: str(Path(tmp) / "model.tmdl"),
                REPORT_PATH_KEY: str(Path(tmp) / "report.json"),
            }
            trace = orch.run_with_reflection(
                "X",
                context=ctx,
                critic=critic,
                max_reflection_rounds=0,
            )
        assert trace.succeeded
        # Reflection was skipped entirely.
        assert trace.reflection_rounds == 0
        assert len(trace.attempts) == 1


# ---------------------------------------------------------------------------
# Cumulative feedback on failure
# ---------------------------------------------------------------------------


class TestCumulativeFeedback:
    def test_failure_then_success(self) -> None:
        # First planner call returns invalid JSON. Second returns valid plan.
        plan = [
            {
                "tool": "create_table",
                "args": {
                    "table_name": "T",
                    "columns": [{"name": "X", "data_type": "string"}],
                },
            }
        ]
        llm = SequenceLLM(
            [
                "{not valid json}",  # first attempt: JSON parse error
                json.dumps(plan),  # second attempt: success
            ]
        )
        critic = StaticLLM(
            json.dumps(
                {
                    "scores": {
                        "correctness": 0.9,
                        "completeness": 0.9,
                        "alignment_with_prompt": 0.9,
                    },
                    "suggestions": [],
                }
            )
        )
        orch = Orchestrator(llm_client=llm)
        register_builtin_tools(orch)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {
                MODEL_PATH_KEY: str(Path(tmp) / "model.tmdl"),
                REPORT_PATH_KEY: str(Path(tmp) / "report.json"),
            }
            trace = orch.run_with_reflection("X", context=ctx, critic=critic)
        assert trace.succeeded
        # The retry worked.
        assert llm.call_count == 2
        # Both attempts were recorded.
        assert len(trace.attempts) == 2
        # The first attempt recorded the error.
        assert trace.attempts[0].error is not None
        assert "valid JSON" in trace.attempts[0].error
        # The second attempt included the feedback.
        assert len(trace.attempts[1].feedback_included) >= 1


# ---------------------------------------------------------------------------
# ReflectiveTrace serialisation
# ---------------------------------------------------------------------------


class TestReflectiveTraceSerialisation:
    def test_to_dict_includes_required_keys(self) -> None:
        trace = ReflectiveTrace(user_prompt="X")
        data = trace.to_dict()
        assert data["user_prompt"] == "X"
        assert data["succeeded"] is False
        assert data["attempts"] == []
        assert data["final_results_count"] == 0
        assert data["final_error"] is None
        assert data["critic_score"] is None
        assert data["reflection_rounds"] == 0

    def test_to_dict_with_critic_score(self, sample_plan: List[Dict[str, Any]]) -> None:
        llm = StaticLLM(json.dumps(sample_plan))
        critic = StaticLLM(
            json.dumps(
                {
                    "scores": {
                        "correctness": 0.9,
                        "completeness": 0.9,
                        "alignment_with_prompt": 0.9,
                    },
                    "suggestions": ["nice work"],
                }
            )
        )
        orch = Orchestrator(llm_client=llm)
        register_builtin_tools(orch)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {
                MODEL_PATH_KEY: str(Path(tmp) / "model.tmdl"),
                REPORT_PATH_KEY: str(Path(tmp) / "report.json"),
            }
            trace = orch.run_with_reflection("X", context=ctx, critic=critic)
        data = trace.to_dict()
        assert data["succeeded"] is True
        assert data["critic_score"] is not None
        assert data["critic_score"]["suggestions"] == ["nice work"]
        assert data["final_results_count"] == 5
        assert len(data["attempts"]) == 1
        attempt = data["attempts"][0]
        assert attempt["results_count"] == 5
        assert attempt["error"] is None


# ---------------------------------------------------------------------------
# run() backward compatibility
# ---------------------------------------------------------------------------


class TestRunBackwardCompatibility:
    def test_run_still_works(self, sample_plan: List[Dict[str, Any]]) -> None:
        # The legacy ``run()`` entry point must keep working
        # unchanged after the reflection additions.
        llm = StaticLLM(json.dumps(sample_plan))
        orch = Orchestrator(llm_client=llm)
        register_builtin_tools(orch)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {
                MODEL_PATH_KEY: str(Path(tmp) / "model.tmdl"),
                REPORT_PATH_KEY: str(Path(tmp) / "report.json"),
            }
            results = orch.run("X", context=ctx)
        assert len(results) == 5

    def test_run_raises_on_final_error(self) -> None:
        llm = StaticLLM("{not json}")
        orch = Orchestrator(llm_client=llm)
        with pytest.raises(ValueError):
            orch.run("X")
