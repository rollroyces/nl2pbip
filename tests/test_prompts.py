"""Tests for the planner prompt module.

Covers:

* Prompt versioning — every shipped prompt has a numeric
  ``PROMPT_VERSION`` and a non-empty ``PROMPT_CHANGELOG`` entry
  that mentions the current changes.

* Report-generation prompt content — the system prompt must
  reference visual selection rules, layout rules, narrative
  composition, and the new visual / measure / layout sections.
  Each rule has a stable keyword we can grep so accidental
  rewrites that drop rules trigger a test failure.

* Legacy prompt availability — the original 9-rule generic
  prompt is still importable so callers can opt out.

* User-message assembly — :func:`build_user_message` prepends
  the user prompt and serialises the payload as indented JSON.

* Feedback builder — :func:`build_feedback_message` produces a
  consistent structure regardless of error type, with type-
  specific guidance for ``TMDLValidationError`` and
  ``PBIRValidationError``.

* Prompt selection — :func:`select_system_prompt` returns the
  focused prompt by default, the focused prompt when
  ``report_focus_enabled=True``, and the legacy prompt when
  ``report_focus_enabled=False``.

* ``prompt_metadata`` — exposes ``version``, ``name``, and a
  ``focused_on_report_generation`` flag.

* Orchestrator integration — the planner payload's
  ``prompt_meta`` block reflects the selected prompt, and the
  system message sent to the LLM is the focused prompt by
  default.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nl2pbip import prompts
from nl2pbip.pbir_validator import PBIRValidationError
from nl2pbip.prompts import (
    LEGACY_GENERIC_SYSTEM_PROMPT,
    PROMPT_CHANGELOG,
    REPORT_GENERATION_PROMPT_VERSION,
    REPORT_GENERATION_SYSTEM_PROMPT,
    build_feedback_message,
    build_user_message,
    prompt_metadata,
    select_system_prompt,
)
from nl2pbip.tmdl_linter import TMDLValidationError

# ----------------------------------------------------------------------
# Versioning
# ----------------------------------------------------------------------


class TestPromptVersioning:
    def test_version_is_a_positive_integer(self) -> None:
        assert isinstance(REPORT_GENERATION_PROMPT_VERSION, int)
        assert REPORT_GENERATION_PROMPT_VERSION >= 1

    def test_changelog_has_entry_for_current_version(self) -> None:
        versions = [entry["version"] for entry in PROMPT_CHANGELOG]
        assert REPORT_GENERATION_PROMPT_VERSION in versions

    def test_current_version_changelog_entry_has_metadata(self) -> None:
        entry = next(
            e
            for e in PROMPT_CHANGELOG
            if e["version"] == REPORT_GENERATION_PROMPT_VERSION
        )
        assert entry["date"], "current entry must have a date"
        assert entry["summary"], "current entry must have a summary"
        assert isinstance(entry["changes"], list)
        assert entry["changes"], "current entry must list changes"

    def test_versions_are_monotonic(self) -> None:
        versions = [entry["version"] for entry in PROMPT_CHANGELOG]
        assert versions == sorted(versions)


# ----------------------------------------------------------------------
# Report-generation prompt content
# ----------------------------------------------------------------------


class TestReportGenerationPromptContent:
    def test_role_framing(self) -> None:
        assert "senior Power BI report designer" in REPORT_GENERATION_SYSTEM_PROMPT

    def test_output_contract_present(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "OUTPUT CONTRACT" in prompt
        assert "valid JSON" in prompt
        assert '"plan"' in prompt

    def test_narrative_composition_section(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "REPORT COMPOSITION" in prompt
        assert "narrative" in prompt
        # Overview / breakdown / detail pattern.
        assert "overview" in prompt
        assert "breakdown" in prompt
        assert "detail" in prompt

    def test_visual_selection_section(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "VISUAL SELECTION" in prompt
        # Data-shape → visual-type mapping rules.
        assert "card" in prompt
        assert "barChart" in prompt
        assert "lineChart" in prompt
        assert "scatterChart" in prompt
        assert "pieChart" in prompt
        assert "pivotTable" in prompt
        assert "tableEx" in prompt
        assert "map" in prompt
        assert "treemap" in prompt

    def test_layout_section(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "LAYOUT" in prompt
        # Layout rules about overlap, slicers, sizing.
        assert "slicer" in prompt
        assert "overlap" in prompt

    def test_measure_visual_pairing_section(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "MEASURES" in prompt or "MEASURE" in prompt
        # Every KPI visual needs an explicit measure.
        assert "KPI" in prompt or "kpi" in prompt
        assert "measure" in prompt.lower()

    def test_filter_section(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "FILTER" in prompt

    def test_tmdl_rules_present(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "TMDL" in prompt
        # Original rules still present.
        assert "dimension tables" in prompt
        assert "fact tables" in prompt
        assert "DAX" in prompt

    def test_security_rules_present(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "RLS" in prompt or "rls" in prompt
        assert "OLS" in prompt or "ols" in prompt
        assert "USERPRINCIPALNAME" in prompt
        assert "CUSTOMDATA" in prompt

    def test_final_package_rule_present(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        assert "package_pbip" in prompt

    def test_use_context_rule_present(self) -> None:
        prompt = REPORT_GENERATION_SYSTEM_PROMPT
        # LLM must use paths / project names / DAX catalog from
        # the payload, not invent them.
        assert "context" in prompt.lower()
        assert "DAX catalog" in prompt or "dax_catalog" in prompt

    def test_prompt_is_substantially_longer_than_legacy(self) -> None:
        # The tuned prompt should be meaningfully richer than
        # the original 9-rule generic prompt.
        assert len(REPORT_GENERATION_SYSTEM_PROMPT) > len(LEGACY_GENERIC_SYSTEM_PROMPT)


# ----------------------------------------------------------------------
# Legacy prompt
# ----------------------------------------------------------------------


class TestLegacyPrompt:
    def test_legacy_prompt_available(self) -> None:
        assert isinstance(LEGACY_GENERIC_SYSTEM_PROMPT, str)
        assert "agentic planner" in LEGACY_GENERIC_SYSTEM_PROMPT

    def test_legacy_prompt_contains_original_rules(self) -> None:
        prompt = LEGACY_GENERIC_SYSTEM_PROMPT
        # Original 9 numbered rules.
        for i in range(1, 10):
            assert f"{i}." in prompt, f"rule {i} missing from legacy prompt"


# ----------------------------------------------------------------------
# User message assembly
# ----------------------------------------------------------------------


class TestBuildUserMessage:
    def test_prepends_user_prompt(self) -> None:
        payload = {"prompt_meta": {"version": 1}}
        msg = build_user_message("Build a sales report", payload)
        assert msg.startswith("Build a sales report")

    def test_serialises_payload_as_indented_json(self) -> None:
        payload = {"a": 1, "b": [1, 2, 3]}
        msg = build_user_message("prompt", payload)
        # The JSON portion must come after the user prompt and
        # contain the indentation we expect.
        json_part = msg.split("\n\n", 1)[1]
        assert json.loads(json_part) == payload
        assert "\n  " in json_part  # 2-space indent from json.dumps(indent=2)

    def test_payload_is_not_mutated(self) -> None:
        original = {"x": 1}
        build_user_message("hi", original)
        assert original == {"x": 1}


# ----------------------------------------------------------------------
# Feedback builder
# ----------------------------------------------------------------------


class TestBuildFeedbackMessage:
    def test_tmdl_validation_error_includes_domain_reminder(self) -> None:
        err = TMDLValidationError("Unknown dataType: foo")
        msg = build_feedback_message(err)
        assert "TMDLValidationError" in msg
        assert "Unknown dataType: foo" in msg
        assert "int64" in msg or "decimal" in msg
        assert "canonical" in msg.lower()
        assert "Adjust the failing tool call" in msg

    def test_pbir_validation_error_includes_domain_reminder(self) -> None:
        err = PBIRValidationError(
            file_type="visualContainer",
            message="Unsupported visualType 'fluffyUnicorn'",
        )
        msg = build_feedback_message(err)
        assert "PBIRValidationError" in msg
        assert "fluffyUnicorn" in msg
        assert "tableEx" in msg
        assert "card" in msg
        assert "Adjust the failing tool call" in msg

    def test_generic_error_returns_base_message(self) -> None:
        msg = build_feedback_message(ValueError("something broke"))
        assert "ValueError" in msg
        assert "something broke" in msg
        assert "Adjust the failing tool call" in msg

    def test_feedback_message_is_self_contained(self) -> None:
        # The feedback message must contain enough context for
        # the LLM to act without needing to look up the
        # original error elsewhere.
        err = TMDLValidationError("bad type")
        msg = build_feedback_message(err)
        assert "error" in msg.lower()
        assert "tool call" in msg.lower()


# ----------------------------------------------------------------------
# Prompt selection
# ----------------------------------------------------------------------


class TestSelectSystemPrompt:
    def test_default_is_focused(self) -> None:
        prompt = select_system_prompt(None)
        assert "report-generation" in prompt
        assert REPORT_GENERATION_SYSTEM_PROMPT in prompt

    def test_default_with_empty_context_is_focused(self) -> None:
        prompt = select_system_prompt({})
        assert "report-generation" in prompt

    def test_focused_when_flag_true(self) -> None:
        prompt = select_system_prompt({"report_focus_enabled": True})
        assert "report-generation" in prompt
        assert REPORT_GENERATION_SYSTEM_PROMPT in prompt

    def test_legacy_when_flag_false(self) -> None:
        prompt = select_system_prompt({"report_focus_enabled": False})
        assert "legacy-generic" in prompt
        assert LEGACY_GENERIC_SYSTEM_PROMPT in prompt

    def test_prompt_includes_version_header(self) -> None:
        prompt = select_system_prompt({})
        assert "[nl2pbip prompt v" in prompt


# ----------------------------------------------------------------------
# Prompt metadata
# ----------------------------------------------------------------------


class TestPromptMetadata:
    def test_default_metadata(self) -> None:
        meta = prompt_metadata(None)
        assert meta["focused_on_report_generation"] is True
        assert meta["name"] == "report-generation"
        assert meta["version"] == REPORT_GENERATION_PROMPT_VERSION

    def test_legacy_metadata(self) -> None:
        meta = prompt_metadata({"report_focus_enabled": False})
        assert meta["focused_on_report_generation"] is False
        assert meta["name"] == "legacy-generic"
        assert meta["version"] == 0

    def test_metadata_keys_are_stable(self) -> None:
        meta = prompt_metadata({})
        # Stable keys — adding new ones is fine but these three
        # are part of the public contract.
        assert set(meta.keys()) >= {
            "version",
            "name",
            "focused_on_report_generation",
        }


# ----------------------------------------------------------------------
# Orchestrator integration
# ----------------------------------------------------------------------


class TestOrchestratorPromptIntegration:
    def test_payload_exposes_prompt_meta(self) -> None:
        from nl2pbip.orchestrator import Orchestrator, register_builtin_tools

        class _Stub:
            def generate(self, messages):
                return '{"plan": []}'

        orch = Orchestrator(llm_client=_Stub())
        register_builtin_tools(orch)
        payload = orch._planner_payload("build report", {})
        assert "prompt_meta" in payload
        assert payload["prompt_meta"]["focused_on_report_generation"] is True

    def test_payload_prompt_meta_reflects_opt_out(self) -> None:
        from nl2pbip.orchestrator import Orchestrator, register_builtin_tools

        class _Stub:
            def generate(self, messages):
                return '{"plan": []}'

        orch = Orchestrator(llm_client=_Stub())
        register_builtin_tools(orch)
        payload = orch._planner_payload("build report", {"report_focus_enabled": False})
        assert payload["prompt_meta"]["focused_on_report_generation"] is False
        assert payload["prompt_meta"]["name"] == "legacy-generic"

    def test_orchestrator_sends_focused_prompt_by_default(self) -> None:
        from nl2pbip.orchestrator import Orchestrator, register_builtin_tools

        captured: dict = {}

        class _Stub:
            def generate(self, messages):
                captured["messages"] = messages
                return '{"plan": []}'

        orch = Orchestrator(llm_client=_Stub())
        register_builtin_tools(orch)
        orch.run("build report", context={})
        sys_msg = captured["messages"][0]["content"]
        assert "report-generation" in sys_msg
        assert REPORT_GENERATION_SYSTEM_PROMPT in sys_msg

    def test_orchestrator_sends_legacy_prompt_when_opted_out(self) -> None:
        from nl2pbip.orchestrator import Orchestrator, register_builtin_tools

        captured: dict = {}

        class _Stub:
            def generate(self, messages):
                captured["messages"] = messages
                return '{"plan": []}'

        orch = Orchestrator(llm_client=_Stub())
        register_builtin_tools(orch)
        orch.run("build report", context={"report_focus_enabled": False})
        sys_msg = captured["messages"][0]["content"]
        assert "legacy-generic" in sys_msg
        assert LEGACY_GENERIC_SYSTEM_PROMPT in sys_msg

    def test_user_message_starts_with_user_text(self) -> None:
        from nl2pbip.orchestrator import Orchestrator, register_builtin_tools

        captured: dict = {}

        class _Stub:
            def generate(self, messages):
                captured["messages"] = messages
                return '{"plan": []}'

        orch = Orchestrator(llm_client=_Stub())
        register_builtin_tools(orch)
        orch.run("Build a quarterly sales report", context={})
        user_msg = captured["messages"][1]["content"]
        assert user_msg.startswith("Build a quarterly sales report")
