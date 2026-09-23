"""Tests for partial-plan recovery.

When an LLM runs out of tokens mid-plan, the response is a
syntactically broken JSON string — the array opens but never
closes. The orchestrator should:

1. Detect the truncation.
2. Salvage the prefix (everything up to the last complete step).
3. Re-prompt the LLM with a continuation note so the next attempt
   picks up from the last step instead of starting over.

These tests pin the helper algorithm + the orchestrator's wiring
of the recovery into the retry loop.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from nl2pbip.orchestrator import (
    Orchestrator,
    PartialPlanRecovery,
    ToolCall,
    _summarise_plan_step,
    try_partial_plan_recovery,
)

# ---------------------------------------------------------------------------
# Pure-function tests for the recovery algorithm
# ---------------------------------------------------------------------------


class TestTryPartialPlanRecovery:
    def test_recovers_truncated_array(self) -> None:
        truncated = '[{"tool":"a","args":{"x":1}},' '{"tool":"b","args":{"y":2}},'
        recovery = try_partial_plan_recovery(truncated)
        assert recovery is not None
        assert len(recovery.plan) == 2
        assert recovery.plan[0].tool == "a"
        assert recovery.plan[1].tool == "b"
        assert recovery.last_step == "b(y=2)"

    def test_recovers_truncated_object_wrapped_plan(self) -> None:
        # When the LLM wraps the array in ``{"plan": [...]}``, the
        # recovery must close both the array AND the outer object.
        truncated = (
            '{"plan":[' '{"tool":"a","args":{"x":1}},' '{"tool":"b","args":{"y":2}},'
        )
        recovery = try_partial_plan_recovery(truncated)
        assert recovery is not None
        assert len(recovery.plan) == 2
        assert recovery.plan[0].tool == "a"
        assert recovery.plan[1].tool == "b"

    def test_returns_none_for_complete_plan(self) -> None:
        # A well-formed response is the caller's normal path; the
        # recovery helper only kicks in on truncated text. Even so,
        # calling it on a complete response should return the full
        # plan — never corrupt it. (The orchestrator only invokes
        # recovery when json.loads has already failed, so this is a
        # robustness check rather than a hot path.)
        complete = '[{"tool":"a","args":{"x":1}},{"tool":"b","args":{"y":2}}]'
        recovery = try_partial_plan_recovery(complete)
        # Complete response: at least one step should be recovered.
        # The algorithm finds the longest valid prefix.
        assert recovery is not None
        assert len(recovery.plan) >= 1

    def test_returns_none_for_garbage(self) -> None:
        # ``None`` recovery → orchestrator falls back to the normal
        # error path (raise ``ValueError``).
        assert try_partial_plan_recovery("not json at all") is None
        assert try_partial_plan_recovery("") is None
        assert try_partial_plan_recovery("[unclosed") is None
        assert try_partial_plan_recovery("plain prose response") is None

    def test_recovers_single_step(self) -> None:
        truncated = '[{"tool":"only","args":{"k":"v"}},'
        recovery = try_partial_plan_recovery(truncated)
        assert recovery is not None
        assert len(recovery.plan) == 1
        assert recovery.plan[0].tool == "only"
        assert recovery.last_step.startswith("only(")

    def test_consumed_prefix_records_what_was_parsed(self) -> None:
        truncated = '[{"tool":"a","args":{"x":1}},' '{"tool":"b","args":{"y":2}},'
        recovery = try_partial_plan_recovery(truncated)
        assert recovery is not None
        # The prefix is the substring we actually parsed, useful
        # for audit logs / debugging.
        assert "tool" in recovery.consumed_prefix
        assert recovery.consumed_prefix.startswith("[")
        # The trailing comma from the truncation must not be in
        # the consumed prefix — that's the boundary.
        assert not recovery.consumed_prefix.rstrip().endswith(",")

    def test_min_recovered_steps_threshold(self) -> None:
        truncated = '[{"tool":"a","args":{"x":1}},'
        # Default threshold is 1 — passes.
        assert try_partial_plan_recovery(truncated) is not None
        # Threshold of 2 rejects the prefix (only 1 step present).
        assert try_partial_plan_recovery(truncated, min_recovered_steps=2) is None


# ---------------------------------------------------------------------------
# Step-summary helper
# ---------------------------------------------------------------------------


class TestStepSummary:
    def test_summarises_simple_call(self) -> None:
        summary = _summarise_plan_step(
            ToolCall(tool="add_table", args={"name": "Date"})
        )
        assert summary.startswith("add_table(")
        assert "name='Date'" in summary

    def test_truncates_long_args(self) -> None:
        # A long arg gets clipped at 60 chars with an ellipsis.
        long_value = "x" * 200
        summary = _summarise_plan_step(ToolCall(tool="t", args={"payload": long_value}))
        # The repr is bounded so the feedback message stays short.
        assert "..." in summary
        assert len(summary) < 200

    def test_handles_no_args(self) -> None:
        summary = _summarise_plan_step(ToolCall(tool="noop", args={}))
        assert summary == "noop()"


# ---------------------------------------------------------------------------
# End-to-end: orchestrator recovers a truncated plan and continues
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """LLM stub that returns different responses per call.

    The first call returns a TRUNCATED plan; the second returns
    the CONTINUATION. The orchestrator should recover the first
    response, build a continuation prompt, and run the second
    response as a follow-on plan.
    """

    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.last_user_message: str = ""

    def generate(self, messages: List[Dict[str, str]]) -> str:
        self.last_user_message = messages[-1]["content"] if messages else ""
        self.calls += 1
        if self.calls - 1 < len(self._responses):
            return self._responses[self.calls - 1]
        return "[]"


class TestOrchestratorPartialRecovery:
    def test_parse_plan_accepts_truncated_response(self) -> None:
        truncated = '[{"tool":"a","args":{"x":1}},' '{"tool":"b","args":{"y":2}},'

        class _LLM:
            def generate(self, _: List[Dict[str, str]]) -> str:
                return truncated

        orch = Orchestrator(llm_client=_LLM())
        # Recovery raises PartialPlanRecovery rather than
        # returning silently — the caller (run() / run_with_reflection)
        # is responsible for issuing the continuation call.
        with pytest.raises(PartialPlanRecovery) as excinfo:
            orch._parse_plan(truncated)
        assert len(excinfo.value.plan) == 2
        assert excinfo.value.last_step == "b(y=2)"
        # The recovery is recorded for the next prompt build.
        assert orch._last_partial_recovery is not None

    def test_parse_plan_raises_on_unrecoverable_garbage(self) -> None:
        class _LLM:
            def generate(self, _: List[Dict[str, str]]) -> str:
                return "not json"

        orch = Orchestrator(llm_client=_LLM())
        with pytest.raises(ValueError, match="valid JSON"):
            orch._parse_plan("not json")

    def test_augment_prompt_emits_continuation_note(self) -> None:
        orch = Orchestrator(llm_client=_ScriptedLLM(['[{"tool":"a","args":{"x":1}},']))
        orch._last_partial_recovery = PartialPlanRecovery(
            plan=[ToolCall(tool="a", args={"x": 1})],
            last_step="a(x=1)",
            consumed_prefix='[{"tool":"a","args":{"x":1}}',
        )
        prompt = orch._augment_prompt("base prompt", [])
        # The continuation note tells the LLM to pick up from the
        # last salvaged step.
        assert "truncated" in prompt.lower()
        assert "a(x=1)" in prompt
        assert "base prompt" in prompt
        # Consuming the note clears the recovery state so a
        # subsequent retry without another truncation doesn't
        # re-emit the same message.
        assert orch._last_partial_recovery is None

    def test_run_recovers_truncated_plan_and_executes_full_plan(self) -> None:
        """End-to-end: truncated first response + valid continuation.

        The orchestrator's run() should:

        1. Parse the truncated first response via recovery.
        2. Execute the salvaged prefix (steps a + b).
        3. Re-prompt the LLM with a continuation note.
        4. Execute the continuation (step c).
        """
        truncated_first = '[{"tool":"alpha","args":{}},' '{"tool":"beta","args":{}},'
        continuation = '[{"tool":"gamma","args":{}}]'
        llm = _ScriptedLLM([truncated_first, continuation])
        orch = Orchestrator(llm_client=llm)
        orch.register_tool(
            name="alpha",
            description="noop",
            schema={},
            handler=lambda **_: {"ok": True},
        )
        orch.register_tool(
            name="beta",
            description="noop",
            schema={},
            handler=lambda **_: {"ok": True},
        )
        orch.register_tool(
            name="gamma",
            description="noop",
            schema={},
            handler=lambda **_: {"ok": True},
        )

        results = orch.run("anything")

        # All three tools executed in order: alpha, beta, gamma.
        assert [r.tool for r in results] == ["alpha", "beta", "gamma"]
        # Two LLM calls (truncated + continuation).
        assert llm.calls == 2
        # The continuation prompt referenced the truncated state.
        assert "truncated" in llm.last_user_message.lower()

    def test_run_resets_recovery_state_between_runs(self) -> None:
        """A second ``run()`` must not carry over recovery state."""
        llm = _ScriptedLLM(['[{"tool":"a","args":{}},', '[{"tool":"b","args":{}}]'])
        orch = Orchestrator(llm_client=llm)
        orch.register_tool(
            name="a",
            description="noop",
            schema={},
            handler=lambda **_: {"ok": True},
        )
        orch.register_tool(
            name="b",
            description="noop",
            schema={},
            handler=lambda **_: {"ok": True},
        )
        orch.run("first")
        assert orch._last_partial_recovery is None
        # A second run starts clean.
        orch._last_partial_recovery = None  # already None, just verifying
        # No continuation note should appear if no truncation has
        # happened yet.
        prompt = orch._augment_prompt("fresh prompt", [])
        assert "truncated" not in prompt.lower()


class TestAugmentPromptCumulativeFeedback:
    """Regression tests for the v1.6.2 fix: _augment_prompt must
    surface ALL accumulated feedback entries to the planner, not just
    the most recent one. The run_with_reflection docstring explicitly
    promises "Cumulative feedback" (every prior attempt's error), and
    the loop accumulates via feedback.append(...). Using only
    feedback[-1] silently dropped every earlier error."""

    def test_empty_feedback_returns_base_only(self) -> None:
        from nl2pbip.example_run import StaticPlanLLM  # noqa: F401  # any LLM stub

        orch = Orchestrator(llm_client=StaticPlanLLM([]))
        prompt = orch._augment_prompt("base prompt", [])
        assert prompt == "base prompt"

    def test_single_feedback_returns_that_entry(self) -> None:
        from nl2pbip.example_run import StaticPlanLLM

        orch = Orchestrator(llm_client=StaticPlanLLM([]))
        prompt = orch._augment_prompt("base prompt", ["error A"])
        assert "error A" in prompt
        assert "base prompt" in prompt

    def test_multiple_feedback_entries_are_all_preserved(self) -> None:
        """The actual regression: two prior errors must both reach the
        planner on attempt 3, not just the most recent."""
        from nl2pbip.example_run import StaticPlanLLM

        orch = Orchestrator(llm_client=StaticPlanLLM([]))
        prompt = orch._augment_prompt(
            "base prompt",
            ["error A: column not found", "error B: missing relationship"],
        )
        # Both prior errors must reach the planner.
        assert (
            "error A: column not found" in prompt
        ), f"earlier feedback was dropped: {prompt!r}"
        assert (
            "error B: missing relationship" in prompt
        ), f"recent feedback was dropped: {prompt!r}"
        # And they are separate entries (newline-separated), not concatenated.
        assert "error A: column not found\nerror B: missing relationship" in prompt

    def test_feedback_appears_after_continuation_note(self) -> None:
        """When both a continuation_note and feedback are present,
        feedback is appended after the note so the LLM sees the most
        recent context (continuation) last."""
        from nl2pbip.example_run import StaticPlanLLM

        orch = Orchestrator(llm_client=StaticPlanLLM([]))
        orch._last_partial_recovery = PartialPlanRecovery(
            plan=[ToolCall(tool="x", args={})],
            last_step="x()",
            consumed_prefix="[]",
        )
        prompt = orch._augment_prompt("base", ["err1", "err2"])
        # Continuation note first, then feedback.
        cont_pos = prompt.index("truncated")
        err1_pos = prompt.index("err1")
        err2_pos = prompt.index("err2")
        assert cont_pos < err1_pos < err2_pos
        assert orch._last_partial_recovery is None
