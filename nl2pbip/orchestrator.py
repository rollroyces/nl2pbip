"""Core orchestration logic for the nl2pbip agentic workflow.

The orchestrator mediates between user prompts, the LLM planner, and the
specialized tooling that materializes PBIP artifacts. The class defined here is
LLM-agnostic; plug in any client that exposes a ``generate`` method compatible
with the ``LLMClient`` protocol below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from nl2pbip.dax_catalog import DAXCatalog
from nl2pbip.packager import package_pbip_handler
from nl2pbip.pbir_engine import (
    add_report_page_handler,
    add_visual_handler,
    set_page_layout_handler,
)
from nl2pbip.pbir_validator import PBIRValidationError
from nl2pbip.tmdl_engine import (
    MODEL_PATH_KEY,
    add_calculation_group_handler,
    add_field_parameter_handler,
    add_measure_handler,
    add_ols_role_handler,
    add_pattern_measure_handler,
    add_power_query_partition_handler,
    add_rls_role_handler,
    create_table_handler,
    define_relationship_handler,
)
from nl2pbip.tmdl_linter import TMDLValidationError


class LLMClient(Protocol):
    """Minimal protocol the orchestrator expects from any LLM client wrapper."""

    def generate(self, messages: List[Dict[str, str]]) -> str:
        """Return the raw model response for the provided chat history."""


ToolHandler = Callable[..., Dict[str, Any]]


@dataclass
class ToolSpec:
    """Describe how the LLM may call a concrete backend tool."""

    name: str
    description: str
    schema: Dict[str, Any]
    handler: ToolHandler

    def validate_payload(self, payload: Dict[str, Any]) -> None:
        """Shallow JSON Schema-ish validation to guard obvious mistakes.

        Supports the top-level ``required`` list, ``anyOf`` (at least one
        of the listed ``required`` branches must satisfy its own
        ``required`` list), and per-property ``type`` checks.
        """

        required = self.schema.get("required", [])
        properties = self.schema.get("properties", {})

        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(
                f"Tool '{self.name}' missing required fields: {', '.join(missing)}"
            )

        # anyOf support: each branch is a sub-schema; the payload must
        # satisfy at least one branch's ``required`` list.
        any_of = self.schema.get("anyOf", [])
        if any_of:
            satisfied = False
            for branch in any_of:
                branch_required = branch.get("required", [])
                if all(key in payload for key in branch_required):
                    satisfied = True
                    break
            if not satisfied:
                # Build a helpful error that lists the alternatives.
                alts = [", ".join(branch.get("required", [])) for branch in any_of]
                raise ValueError(
                    f"Tool '{self.name}' requires one of: " + " | ".join(alts)
                )

        for key, value in payload.items():
            if key not in properties:
                raise ValueError(
                    f"Tool '{self.name}' does not accept argument '{key}'."
                )
            expected = properties[key].get("type")
            if expected and not _matches_type(expected, value):
                raise TypeError(
                    f"Tool '{self.name}' expected '{key}' to be of type {expected}."
                )


@dataclass
class ToolCall:
    """Parsed tool call emitted by the planner."""

    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    rationale: Optional[str] = None


@dataclass
class ToolResult:
    """Capture execution metadata for each tool call."""

    tool: str
    args: Dict[str, Any]
    output: Dict[str, Any]


@dataclass
class PlannerClarification(Exception):
    """Raised when the planner emits a clarification question instead of a plan.

    The LLM-driven planner may decide that the user's prompt is
    ambiguous (e.g. multiple plausible data sources, conflicting
    metric definitions). Rather than guessing, it returns a
    ``{"clarification": "..."}`` payload. The orchestrator surfaces
    the question via this exception so the caller can present it
    back to the user, gather an answer, and re-invoke the planner.
    """

    question: str
    rationale: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.rationale:
            return f"{self.question} (rationale: {self.rationale})"
        return self.question


@dataclass
class AttemptRecord:
    """One attempt at generating + executing a plan.

    The orchestrator stores every attempt in a :class:`ReflectiveTrace`
    so callers can inspect what the LLM produced, what tools ran,
    and what (if anything) failed.
    """

    attempt: int
    plan: List[ToolCall]
    results: List[ToolResult] = field(default_factory=list)
    error: Optional[str] = None
    feedback_included: List[str] = field(default_factory=list)
    reflection: Optional[str] = None


@dataclass
class PlanQualityScore:
    """Critic-pass scoring of a successful plan.

    Parsed from the LLM critic response. All scores are in [0, 1].
    ``suggestions`` is a list of improvement ideas the orchestrator
    can act on (or surface to the user) in a follow-up turn.
    """

    correctness: float = 0.0
    completeness: float = 0.0
    alignment_with_prompt: float = 0.0
    suggestions: List[str] = field(default_factory=list)
    raw_response: str = ""

    @property
    def overall(self) -> float:
        """Weighted average; correctness counts more than completeness."""
        return (
            0.5 * self.correctness
            + 0.3 * self.completeness
            + 0.2 * self.alignment_with_prompt
        )

    def is_acceptable(self, threshold: float = 0.7) -> bool:
        """Default threshold: overall >= 0.7 + correctness >= 0.6."""
        return self.overall >= threshold and self.correctness >= 0.6


@dataclass
class ReflectiveTrace:
    """Full record of a ``run_with_reflection`` invocation."""

    user_prompt: str
    attempts: List[AttemptRecord] = field(default_factory=list)
    final_results: Optional[List[ToolResult]] = None
    final_error: Optional[str] = None
    critic_score: Optional[PlanQualityScore] = None
    reflection_rounds: int = 0

    @property
    def succeeded(self) -> bool:
        return self.final_error is None and self.final_results is not None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for logging / persistence."""
        return {
            "user_prompt": self.user_prompt,
            "succeeded": self.succeeded,
            "attempts": [
                {
                    "attempt": a.attempt,
                    "plan": [
                        {"tool": c.tool, "args": c.args, "rationale": c.rationale}
                        for c in a.plan
                    ],
                    "results_count": len(a.results),
                    "error": a.error,
                    "feedback_included": a.feedback_included,
                    "reflection": a.reflection,
                }
                for a in self.attempts
            ],
            "final_results_count": (
                len(self.final_results) if self.final_results else 0
            ),
            "final_error": self.final_error,
            "critic_score": (
                {
                    "correctness": self.critic_score.correctness,
                    "completeness": self.critic_score.completeness,
                    "alignment_with_prompt": (self.critic_score.alignment_with_prompt),
                    "overall": self.critic_score.overall,
                    "suggestions": self.critic_score.suggestions,
                }
                if self.critic_score
                else None
            ),
            "reflection_rounds": self.reflection_rounds,
        }


class ToolRegistry:
    """Bidirectional lookup for tool specifications."""

    def __init__(self) -> None:
        self._registry: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._registry:
            raise ValueError(f"Tool '{spec.name}' already registered.")
        self._registry[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._registry[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool '{name}'.") from exc

    def all_specs(self) -> List[ToolSpec]:
        return list(self._registry.values())


class Orchestrator:
    """Own the plan→act loop for translating NL prompts into PBIP assets."""

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: Optional[ToolRegistry] = None,
        dax_catalog: Optional[DAXCatalog] = None,
    ) -> None:
        self._llm = llm_client
        self._tools = tool_registry or ToolRegistry()
        self._dax_catalog = dax_catalog

    def register_tool(
        self, name: str, description: str, schema: Dict[str, Any], handler: ToolHandler
    ) -> None:
        """Public helper for wiring tool implementations in bootstrapping code."""

        self._tools.register(
            ToolSpec(name=name, description=description, schema=schema, handler=handler)
        )

    def run(
        self, user_prompt: str, context: Optional[Dict[str, Any]] = None
    ) -> List[ToolResult]:
        """Full cycle with validation-aware self correction."""

        context = context or {}
        feedback: List[str] = []
        max_attempts = 3
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            effective_prompt = self._augment_prompt(user_prompt, feedback)
            plan_response = self._request_plan(effective_prompt, context)
            plan = self._parse_plan(plan_response)
            try:
                return self._execute_plan(plan, context)
            except (
                TMDLValidationError,
                PBIRValidationError,
                ValueError,
                TypeError,
            ) as exc:
                last_error = exc
                feedback.append(self._feedback_for_exception(exc))
                if attempt == max_attempts:
                    break
        if last_error is not None:
            raise last_error
        raise RuntimeError("Planner retries exceeded without validation detail.")

    def run_with_reflection(
        self,
        user_prompt: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        max_attempts: int = 3,
        max_reflection_rounds: int = 1,
        critic_threshold: float = 0.7,
        critic: Optional[LLMClient] = None,
    ) -> ReflectiveTrace:
        """Agentic loop with persistent trace + post-success critic pass.

        Compared to :meth:`run`, this method:

        1. **Persistent trace.** Every attempt (plan, results, error,
           feedback) is recorded in a :class:`ReflectiveTrace` that
           the caller can inspect, persist, or stream to a UI.
        2. **Cumulative feedback.** When an attempt fails, the next
           prompt includes ALL prior errors (not just the most
           recent one), so the LLM can avoid repeating earlier
           mistakes.
        3. **Post-success critic.** After a successful run, the
           orchestrator invokes ``critic`` (or the same LLM if
           ``critic=None``) with a reflection prompt asking the
           LLM to score the plan on ``correctness``,
           ``completeness``, and ``alignment_with_prompt``. The
           parsed :class:`PlanQualityScore` is attached to the
           trace. If the score is below ``critic_threshold``, the
           orchestrator re-invokes the planner with the critic's
           ``suggestions`` as feedback, up to
           ``max_reflection_rounds`` times.
        4. **Clarification handling.** If the planner emits a
           ``{"clarification": "..."}`` payload, the orchestrator
           stops and surfaces :class:`PlannerClarification` via the
           trace's ``final_error`` field (without consuming a retry).

        Parameters
        ----------
        user_prompt
            The natural-language request.
        context
            Optional planner context (model path, data sources, etc.).
        max_attempts
            Maximum planner attempts before giving up on the initial
            plan. The critic pass runs after the first success.
        max_reflection_rounds
            Maximum follow-up planner invocations triggered by a
            low-score critic pass. Each round consumes one more
            attempt (so the total LLM calls is bounded by
            ``max_attempts + max_reflection_rounds``).
        critic_threshold
            Minimum overall score for the critic to accept a plan
            without another reflection round.
        critic
            Optional separate LLM client for the critic pass. If
            ``None``, the planner's LLM is reused.

        Returns
        -------
        ReflectiveTrace
            The full attempt history, final results (or final error),
            and critic score.
        """
        context = context or {}
        trace = ReflectiveTrace(user_prompt=user_prompt)
        feedback: List[str] = []
        critic_client = critic or self._llm
        last_error: Optional[Exception] = None
        successful_results: Optional[List[ToolResult]] = None
        max_total = max_attempts + max_reflection_rounds
        for attempt in range(1, max_total + 1):
            effective_prompt = self._augment_prompt(user_prompt, feedback)
            plan_response = self._request_plan(effective_prompt, context)
            record = AttemptRecord(
                attempt=attempt,
                plan=[],
                feedback_included=list(feedback),
            )
            try:
                plan = self._parse_plan(plan_response)
            except PlannerClarification as exc:
                # The planner wants more information; surface it
                # via the trace and stop.
                record.error = f"clarification: {exc.question}"
                trace.attempts.append(record)
                trace.final_error = (
                    f"Planner requested clarification: {exc.question}"
                    + (f" (rationale: {exc.rationale})" if exc.rationale else "")
                )
                return trace
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                # Plan parsing / JSON-shape errors should consume a
                # retry the same way execution errors do, so the
                # LLM can self-correct on the next attempt.
                last_error = exc
                feedback_str = self._feedback_for_exception(exc)
                feedback.append(feedback_str)
                record.error = feedback_str
                trace.attempts.append(record)
                if attempt == max_total:
                    break
                continue
            record.plan = plan
            try:
                results = self._execute_plan(plan, context)
                record.results = results
                trace.attempts.append(record)
                successful_results = results
                break
            except (
                TMDLValidationError,
                PBIRValidationError,
                ValueError,
                TypeError,
            ) as exc:
                last_error = exc
                feedback_str = self._feedback_for_exception(exc)
                feedback.append(feedback_str)
                record.error = feedback_str
                trace.attempts.append(record)
                if attempt == max_attempts:
                    break
        if successful_results is None:
            # All attempts failed.
            trace.final_error = (
                str(last_error)
                if last_error
                else ("Planner retries exceeded without validation detail.")
            )
            return trace
        trace.final_results = successful_results
        # Critic pass.
        score = self._critic_score(
            user_prompt, successful_results, trace, critic_client
        )
        trace.critic_score = score
        # Reflection loop — keep refining until score is acceptable
        # or max_reflection_rounds is exhausted.
        while (
            score is not None
            and not score.is_acceptable(critic_threshold)
            and trace.reflection_rounds < max_reflection_rounds
        ):
            trace.reflection_rounds += 1
            # Build feedback from the critic's suggestions.
            reflection_feedback = self._build_reflection_feedback(score)
            feedback.append(reflection_feedback)
            reflection_prompt = self._augment_prompt(user_prompt, feedback)
            record = AttemptRecord(
                attempt=len(trace.attempts) + 1,
                plan=[],
                feedback_included=list(feedback),
                reflection=reflection_feedback,
            )
            try:
                plan_response = self._request_plan(reflection_prompt, context)
                plan = self._parse_plan(plan_response)
            except PlannerClarification as exc:
                record.error = f"clarification: {exc.question}"
                trace.attempts.append(record)
                break
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                # Reflection-plan parsing error — break out, keep
                # the prior successful results.
                record.error = self._feedback_for_exception(exc)
                trace.attempts.append(record)
                break
            record.plan = plan
            try:
                results = self._execute_plan(plan, context)
                record.results = results
                trace.attempts.append(record)
                trace.final_results = results
                # Re-score.
                score = self._critic_score(user_prompt, results, trace, critic_client)
                trace.critic_score = score
            except (
                TMDLValidationError,
                PBIRValidationError,
                ValueError,
                TypeError,
            ) as exc:
                record.error = self._feedback_for_exception(exc)
                trace.attempts.append(record)
                # Reflection that errors breaks the loop — caller
                # already has the prior successful results.
                break
        return trace

    def _critic_score(
        self,
        user_prompt: str,
        results: List[ToolResult],
        trace: ReflectiveTrace,
        critic: LLMClient,
    ) -> Optional[PlanQualityScore]:
        """Invoke the critic LLM and parse its JSON response.

        Returns ``None`` if the critic output cannot be parsed.
        """
        from nl2pbip.prompts import (
            CRITIC_SYSTEM_PROMPT,
            build_critic_user_message,
        )

        critic_messages = [
            {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_critic_user_message(
                    user_prompt, results, trace.attempts
                ),
            },
        ]
        try:
            raw = critic.generate(critic_messages)
        except Exception:  # pragma: no cover - defensive
            return None
        return self._parse_critic_score(raw)

    @staticmethod
    def _parse_critic_score(raw: str) -> Optional[PlanQualityScore]:
        """Parse the critic's JSON response into a :class:`PlanQualityScore`.

        Tolerates markdown-fenced JSON (``\\`\\`\\`json ... \\`\\`\\`\\``).
        Returns ``None`` if no parseable JSON is found.
        """
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        # Strip a leading markdown fence if present.
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1 :]
            if text.endswith("```"):
                text = text[:-3].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        # Tolerate either ``{"scores": {...}, "suggestions": [...]}``
        # or a flat shape with the keys at the top level.
        scores = data.get("scores", data)
        if not isinstance(scores, dict):
            return None
        try:
            correctness = float(scores.get("correctness", 0.0))
            completeness = float(scores.get("completeness", 0.0))
            alignment = float(
                scores.get("alignment_with_prompt", scores.get("alignment", 0.0))
            )
        except (TypeError, ValueError):
            return None
        suggestions = data.get("suggestions", [])
        if isinstance(suggestions, str):
            suggestions = [suggestions]
        if not isinstance(suggestions, list):
            suggestions = []
        return PlanQualityScore(
            correctness=max(0.0, min(1.0, correctness)),
            completeness=max(0.0, min(1.0, completeness)),
            alignment_with_prompt=max(0.0, min(1.0, alignment)),
            suggestions=[str(s) for s in suggestions if isinstance(s, str)],
            raw_response=raw,
        )

    @staticmethod
    def _build_reflection_feedback(score: PlanQualityScore) -> str:
        """Build a feedback string the planner can act on next round."""
        lines = [
            "The previous plan was reviewed by a critic and scored:",
            f"  - correctness: {score.correctness:.2f}",
            f"  - completeness: {score.completeness:.2f}",
            f"  - alignment_with_prompt: {score.alignment_with_prompt:.2f}",
            f"  - overall: {score.overall:.2f}",
        ]
        if score.suggestions:
            lines.append("")
            lines.append("Critic suggestions (apply on the next attempt):")
            for idx, suggestion in enumerate(score.suggestions, 1):
                lines.append(f"  {idx}. {suggestion}")
        else:
            lines.append("")
            lines.append("Improve the plan along the lowest-scoring axis.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Planning helpers
    # ------------------------------------------------------------------
    def _request_plan(self, user_prompt: str, context: Dict[str, Any]) -> str:
        from nl2pbip.prompts import build_user_message, select_system_prompt

        system_prompt = select_system_prompt(context)
        user_message = build_user_message(
            user_prompt, self._planner_payload(user_prompt, context)
        )
        planning_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        return self._llm.generate(planning_messages)

    def _parse_plan(self, plan_text: str) -> List[ToolCall]:
        try:
            raw_plan = json.loads(plan_text)
        except json.JSONDecodeError as exc:
            raise ValueError("Planner must return valid JSON.") from exc

        if isinstance(raw_plan, dict) and "plan" in raw_plan:
            raw_plan = raw_plan["plan"]

        # Clarification mode: the planner emits a top-level
        # ``{"clarification": "...", "rationale": "..."}`` object
        # instead of a plan list when it needs more information.
        if isinstance(raw_plan, dict) and "clarification" in raw_plan:
            raise PlannerClarification(
                question=raw_plan["clarification"],
                rationale=raw_plan.get("rationale"),
            )

        if not isinstance(raw_plan, list):
            raise ValueError(
                "Planner output must be a JSON array of tool calls or an object with a 'plan' array."
            )

        plan: List[ToolCall] = []
        for step in raw_plan:
            tool = step.get("tool")
            args = step.get("args", {})
            rationale = step.get("reason")
            if not tool:
                raise ValueError("Each plan step needs a 'tool' field.")
            if not isinstance(args, dict):
                raise ValueError("Plan 'args' must be an object.")
            plan.append(ToolCall(tool=tool, args=args, rationale=rationale))
        return plan

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------
    def _execute_plan(
        self, plan: List[ToolCall], context: Dict[str, Any]
    ) -> List[ToolResult]:
        results: List[ToolResult] = []
        for call in plan:
            spec = self._tools.get(call.tool)
            spec.validate_payload(call.args)
            # inject shared context when desired by the handler
            payload = {**call.args, "context": context}
            output = spec.handler(**payload)
            results.append(ToolResult(tool=call.tool, args=call.args, output=output))
        return results

    def _tool_stub(self, spec: ToolSpec) -> Dict[str, Any]:
        return {
            "name": spec.name,
            "description": spec.description,
            "schema": spec.schema,
        }

    def _planner_payload(
        self, user_prompt: str, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        from nl2pbip.prompts import prompt_metadata

        payload: Dict[str, Any] = {
            "prompt": user_prompt,
            "context": context,
            "tools": [self._tool_stub(spec) for spec in self._tools.all_specs()],
            # Expose which system prompt version the LLM received so
            # callers / audit logs / regression tests can pin what
            # produced a given plan.
            "prompt_meta": prompt_metadata(context),
        }
        # Include a snapshot of the current model state so the LLM
        # can produce accurate relationships without guessing at
        # table/column names. Without this, an LLM asked to wire
        # ``Sales → Date`` typically invents column names that don't
        # exist on the tables it just created.
        model_summary = self._summarise_model(context)
        if model_summary is not None:
            payload["model_state"] = model_summary
        # Profile any data sources the caller has registered so
        # the LLM sees actual values, distinct counts, and column
        # ranges before designing relationships or visuals.
        # Without this, an LLM asked for "revenue per region"
        # has no idea what values ``Region`` actually takes.
        data_summary = self._summarise_data_sources(context)
        if data_summary is not None:
            payload["data_profile"] = data_summary
            # When an LLM client is available AND the caller hasn't
            # opted out, also ask the LLM to enrich the deterministic
            # profile with column-semantics, measure, and visual
            # suggestions. The deterministic summary above is
            # always kept — the AI advisor only ADDS hints, never
            # replaces the structural facts.
            ai_summary = self._summarise_ai_schema(context, data_summary)
            if ai_summary is not None:
                payload["ai_schema_hints"] = ai_summary
            # Curated ontology (schema.org + PROV-O) — maps common
            # column names to typed vocabulary entries. This gives
            # the LLM a real anchor to pick from rather than
            # inventing column semantics from scratch. Disabled
            # by context["ontology_hints_enabled"] = False.
            if context.get("ontology_hints_enabled", True) is not False:
                ontology_summary = self._summarise_ontology(data_summary)
                if ontology_summary is not None:
                    payload["ontology_hints"] = ontology_summary
            # Cross-table data understanding — primary keys,
            # FK coverage stats (matching / orphan counts),
            # cardinality hints, numeric distributions, time
            # ranges. The LLM uses these to design relationships
            # and measures with concrete numbers, not guesses.
            understanding = self._summarise_data_understanding(context, data_summary)
            if understanding is not None:
                payload["data_understanding"] = understanding
        if self._dax_catalog:
            payload["dax_catalog"] = self._dax_catalog.prompt_payload()
        return payload

    def _summarise_data_understanding(
        self,
        context: Dict[str, Any],
        data_summary: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Cross-table data understanding for the planner payload.

        Builds PK / FK-coverage / distribution / time-range
        statistics that the LLM can use to design relationships
        and measures with concrete numbers rather than guesses.

        Disabled by ``context["data_understanding_enabled"] = False``.

        Returns ``None`` when ``data_sources`` isn't set (the
        data needed to compute the stats isn't available).
        """
        if context.get("data_understanding_enabled", True) is False:
            return None
        # Re-import here to keep the cold-start path slim.
        from nl2pbip.data_inspector import inspect_data_sources
        from nl2pbip.data_understanding import analyze_data_understanding

        sources = context.get("data_sources")
        if not isinstance(sources, dict) or not sources:
            return None
        try:
            profiles = inspect_data_sources(
                sources,
                max_rows_per_source=int(context.get("data_profile_max_rows", 1000)),
            )
        except Exception:
            return None
        understanding = analyze_data_understanding(profiles, records_by_source=sources)
        return understanding.to_json()

    def _summarise_ontology(
        self, data_summary: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Build an ontology-hints summary from the data profile.

        Walks every column in the deterministic profile, asks the
        curated ontology for fuzzy-matched suggestions, and emits
        a JSON-serialisable block the LLM can use as a vocabulary
        anchor. Disabled by ``context["ontology_hints_enabled"] =
        False``.

        Returns ``None`` when no columns have ontology matches (the
        LLM doesn't need empty blocks).
        """
        from nl2pbip.ontology import build_planner_summary

        # Collect every column name across every source/table.
        column_names: List[str] = []
        for source_entry in data_summary.get("tables", []) or []:
            for table_data in source_entry.get("tables", []) or []:
                for col in table_data.get("columns", []) or []:
                    name = col.get("name")
                    if isinstance(name, str) and name:
                        column_names.append(name)
        if not column_names:
            return None
        summary = build_planner_summary(column_names)
        # If no column has a match, the LLM gets nothing useful —
        # skip the block to keep the planner payload small.
        if not summary["columns"]:
            return None
        return summary

    def _summarise_ai_schema(
        self,
        context: Dict[str, Any],
        data_summary: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Run the AI schema advisor over the deterministic profile.

        Returns ``None`` when:

        * ``context["data_inspector_ai_enabled"]`` is explicitly
          ``False`` (caller opted out).
        * ``context["data_sources"]`` isn't set (no profile to
          enrich — handled by the caller, but defensive here).
        * The LLM call fails or returns unparseable output.

        The advisor's input is the deterministic profile only —
        not raw row data — so the planner payload stays bounded
        by the number of *columns*, not the number of *rows*.
        """
        if context.get("data_inspector_ai_enabled", True) is False:
            return None
        # Local import to keep the cold-start path slim — most
        # callers don't use data sources and never reach this.
        from nl2pbip.schema_advisor import SchemaAdvisor

        # Rebuild the typed profiles from the summary so the
        # advisor can iterate columns without re-inspecting the
        # raw source. The deterministic profile carries everything
        # the advisor needs.
        profiles = self._rehydrate_profiles(data_summary)
        if not profiles:
            return None
        advisor = SchemaAdvisor(self._llm)
        result = advisor.advise(profiles)
        if result is None:
            return None
        return result.to_json()

    def _rehydrate_profiles(self, data_summary: Dict[str, Any]) -> List[Any]:
        """Reconstruct :class:`DataProfile` objects from a planner summary.

        The planner payload's ``data_profile`` block is the JSON
        shape of a :class:`DataProfile` minus the in-memory
        Python type machinery. Reconstructing here lets the
        advisor iterate the same structural fields the inspector
        populated, without re-reading the original data source.
        """
        from nl2pbip.data_inspector import (
            ColumnProfile,
            DataProfile,
            TableProfile,
        )

        profiles: List[DataProfile] = []
        for source_entry in data_summary.get("tables", []) or []:
            table_profiles: List[TableProfile] = []
            for table_data in source_entry.get("tables", []) or []:
                cols = [
                    ColumnProfile(
                        name=col_data.get("name", ""),
                        inferred_type=col_data.get("inferred_type", "text"),
                        non_null_count=int(col_data.get("non_null_count", 0)),
                        distinct_count=int(col_data.get("distinct_count", 0)),
                        null_rate=float(col_data.get("null_rate", 0.0)),
                        min=col_data.get("min"),
                        max=col_data.get("max"),
                        mean=col_data.get("mean"),
                        median=col_data.get("median"),
                        stddev=col_data.get("stddev"),
                        min_date=col_data.get("min_date"),
                        max_date=col_data.get("max_date"),
                        distinct_examples=list(
                            col_data.get("distinct_examples", []) or []
                        ),
                    )
                    for col_data in table_data.get("columns", []) or []
                ]
                table_profiles.append(
                    TableProfile(
                        name=table_data.get("name", ""),
                        row_count=int(table_data.get("row_count", 0)),
                        sampled_at_least=int(table_data.get("sampled_at_least", 0)),
                        columns=cols,
                    )
                )
            profiles.append(
                DataProfile(
                    source_name=source_entry.get("source_name", ""),
                    source_kind=source_entry.get("source_kind", "records"),
                    tables=table_profiles,
                    warnings=list(source_entry.get("warnings", []) or []),
                )
            )
        return profiles

    def _summarise_data_sources(
        self, context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Profile registered data sources and emit JSON summary.

        The orchestrator's caller can register data sources via
        ``context["data_sources"]`` (a ``{name: source}`` mapping
        where ``source`` is a CSV/JSON/Parquet path, an in-memory
        list of records, or a callable returning a DataFrame-like
        object). The summary includes per-column type inference,
        distinct counts, top examples, and numeric/date range stats
        — plus heuristic relationship suggestions so the LLM has
        candidate foreign-key endpoints ready.

        Returns ``None`` when no data sources are registered, or
        when all sources fail to load. The summary is intentionally
        compact (top-5 examples per column) so the planner payload
        stays bounded.
        """
        sources = context.get("data_sources")
        if not isinstance(sources, dict) or not sources:
            return None
        # Local import to avoid pulling the inspector onto the
        # cold-start path of every orchestrator operation.
        from nl2pbip.data_inspector import (
            inspect_data_sources,
            suggest_relationships,
        )

        max_rows = int(context.get("data_profile_max_rows", 1000))
        redact = bool(context.get("data_profile_redact_values", False))
        try:
            profiles = inspect_data_sources(
                sources,
                max_rows_per_source=max_rows,
                redact_distinct_values=redact,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "tables": [],
                "warnings": [f"data_inspector failed: {exc}"],
                "suggested_relationships": [],
            }

        # Heuristic relationship suggestions across profiled
        # tables. These are ranked hints, not verified joins; the
        # actual ``define_relationship`` call goes through the
        # validation we added in PR #5.
        suggestions = suggest_relationships(profiles)
        return {
            "tables": [
                profile.to_json(redact_distinct_values=redact) for profile in profiles
            ],
            "suggested_relationships": suggestions,
        }

    def _summarise_model(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a JSON-serialisable summary of the current model state.

        Includes each table's columns with their data types and the
        list of existing relationships. ``None`` is returned when
        no model file is set in the context (first iteration of an
        empty workspace) or the file doesn't exist yet.

        The summary is intentionally compact — the LLM doesn't need
        the full TMDL text, just the schema shape — but it's rich
        enough to detect foreign-key column conventions like
        ``<table>_id`` / ``<table>Id`` and pick the right endpoint
        for a relationship.
        """
        from pathlib import Path as _Path

        model_path_str = context.get(MODEL_PATH_KEY)
        if not isinstance(model_path_str, str):
            return None
        model_path = _Path(model_path_str)
        if not model_path.exists():
            return None
        try:
            from nl2pbip.tmdl_engine import load_model

            model = load_model(model_path)
        except (FileNotFoundError, OSError, ValueError):
            return None

        tables_summary: Dict[str, Dict[str, Any]] = {}
        for table_name, table in model.tables.items():
            tables_summary[table_name] = {
                "columns": {
                    col_name: col.data_type for col_name, col in table.columns.items()
                }
            }
        relationships_summary = [
            {
                "name": rel.name,
                "from": f"{rel.from_table}[{rel.from_column}]",
                "to": f"{rel.to_table}[{rel.to_column}]",
                "cardinality": rel.cardinality,
                "active": rel.is_active,
            }
            for rel in model.relationships
        ]
        return {
            "tables": tables_summary,
            "relationships": relationships_summary,
            "table_count": len(model.tables),
            "relationship_count": len(model.relationships),
        }

    def _augment_prompt(self, base_prompt: str, feedback: List[str]) -> str:
        if not feedback:
            return base_prompt
        return f"{base_prompt}\n\n{feedback[-1]}"

    def _feedback_for_exception(self, error: Exception) -> str:
        from nl2pbip.prompts import build_feedback_message

        return build_feedback_message(error)


# ----------------------------------------------------------------------
# Built-in tool registry
# ----------------------------------------------------------------------
DEFAULT_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "add_report_page",
        "description": "Create a PBIR page canvas with optional theme tokens.",
        "schema": {
            "type": "object",
            "required": ["page"],
            "properties": {
                "page": {"type": "string"},
                "display_name": {"type": "string"},
                "size": {"type": "object"},
                "theme_tokens": {"type": "object"},
                "background": {"type": "object"},
            },
        },
        "handler": add_report_page_handler,
    },
    {
        "name": "create_table",
        "description": "Author a new TMDL table with typed columns and measures.",
        "schema": {
            "type": "object",
            "required": ["table_name", "columns"],
            "properties": {
                "table_name": {"type": "string"},
                "source": {"type": "string"},
                "columns": {"type": "array"},
                "measures": {"type": "array"},
            },
        },
        "handler": create_table_handler,
    },
    {
        "name": "add_measure",
        "description": "Add or update a DAX measure on an existing table.",
        "schema": {
            "type": "object",
            "required": ["table_name", "measure_name", "expression"],
            "properties": {
                "table_name": {"type": "string"},
                "measure_name": {"type": "string"},
                "expression": {"type": "string"},
                "format_string": {"type": "string"},
            },
        },
        "handler": add_measure_handler,
    },
    {
        "name": "add_pattern_measure",
        "description": "Create a measure from a cataloged DAX pattern.",
        "schema": {
            "type": "object",
            "required": ["table_name", "measure_name", "pattern_key", "base_measure"],
            "properties": {
                "table_name": {"type": "string"},
                "measure_name": {"type": "string"},
                "pattern_key": {"type": "string"},
                "base_measure": {"type": "string"},
                "format_override": {"type": "string"},
                "parameters": {"type": "object"},
            },
        },
        "handler": add_pattern_measure_handler,
    },
    {
        "name": "define_relationship",
        "description": "Create or overwrite a semantic relationship between tables.",
        "schema": {
            "type": "object",
            "required": ["from_table", "from_column", "to_table", "to_column"],
            "properties": {
                "from_table": {"type": "string"},
                "from_column": {"type": "string"},
                "to_table": {"type": "string"},
                "to_column": {"type": "string"},
                "cardinality": {"type": "string"},
                "cross_filter_direction": {"type": "string"},
                "active": {"type": "boolean"},
            },
        },
        "handler": define_relationship_handler,
    },
    {
        "name": "add_calculation_group",
        "description": (
            "Add a calculation-group table to the semantic model. "
            "Calculation groups collapse redundant time-intelligence measures "
            "into a single slicer. Either pass `group_key` to materialise a "
            "predefined catalog group, or pass an explicit `items` list to "
            "define the group inline. Dynamic format strings may be provided "
            "via `format_string_definitions` (name → DAX expression)."
        ),
        "schema": {
            "type": "object",
            "required": [],
            "anyOf": [
                {"required": ["group_key"]},
                {"required": ["items"]},
            ],
            "properties": {
                "group_key": {
                    "type": "string",
                    "description": (
                        "Key of a calculation group registered in "
                        "`dax_library.json`. Used unless `items` is "
                        "provided."
                    ),
                },
                "table_name": {
                    "type": "string",
                    "description": (
                        "Override the catalog's default table name. "
                        "If neither `table_name` nor the catalog entry "
                        "supplies one, the handler fails with a clear "
                        "error."
                    ),
                },
                "precedence": {
                    "type": "integer",
                    "description": (
                        "Calculation group precedence (lower numbers "
                        "evaluate first). Defaults to the catalog value."
                    ),
                },
                "items": {
                    "type": "array",
                    "description": (
                        "Inline list of calculation items, used instead of "
                        "the catalog. Each item is {name, expression, "
                        "format_string?, format_string_definition?, "
                        "description?}."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["name", "expression"],
                        "properties": {
                            "name": {"type": "string"},
                            "expression": {"type": "string"},
                            "format_string": {"type": "string"},
                            "format_string_definition": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "format_string_definitions": {
                    "type": "object",
                    "description": (
                        "Optional name → DAX expression map applied to "
                        "items that don't carry their own "
                        "format_string_definition."
                    ),
                    "additionalProperties": {"type": "string"},
                },
            },
        },
        "handler": add_calculation_group_handler,
    },
    {
        "name": "add_field_parameter",
        "description": (
            "Create a Power BI field-parameter table (a disconnected "
            "calculated table that drives dynamic measure / column / "
            "table selection via a slicer). Power BI Desktop emits the "
            "result as a calculated table with `isParameterTable`, three "
            "columns (Name / Fields / Ordinal), and a DAX table "
            "expression using NAMEOF(). Members reference either a "
            "column (`table_name` + `column_name`) or a measure "
            "(`table_name` + `measure_name`); the two are mutually "
            "exclusive per member."
        ),
        "schema": {
            "type": "object",
            "required": ["parameter_name", "members"],
            "properties": {
                "parameter_name": {
                    "type": "string",
                    "description": (
                        "Name of the parameter table. Also the name "
                        "of the user-visible Name column and the "
                        "prefix of the Fields / Ordinal columns."
                    ),
                },
                "members": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "Field-parameter members. Each is "
                        "{display_name, table_name, column_name} for "
                        "a column reference or "
                        "{display_name, table_name, measure_name} "
                        "for a measure reference."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["display_name", "table_name"],
                        "anyOf": [
                            {"required": ["column_name"]},
                            {"required": ["measure_name"]},
                        ],
                        "properties": {
                            "display_name": {"type": "string"},
                            "table_name": {"type": "string"},
                            "column_name": {"type": "string"},
                            "measure_name": {"type": "string"},
                        },
                    },
                },
                "sort_by_column_name": {
                    "type": "string",
                    "description": (
                        "Override the default Ordinal-column sort. "
                        "Defaults to `<parameter_name> Ordinal`."
                    ),
                },
            },
        },
        "handler": add_field_parameter_handler,
    },
    {
        "name": "add_power_query_partition",
        "description": (
            "Add (or replace) a partition whose source is a Power Query "
            "M expression. Two modes: 'template' (csv / sql / json / "
            "sharepoint / odata / web) with 'params' (template-specific "
            "arguments), or 'm_expression' (verbatim M query string). "
            "The 'promote' flag wraps the staging query in the standard "
            "Table.PromoteHeaders + Table.TransformColumnTypes pattern. "
            "Use 'replace=true' to overwrite an existing partition of "
            "the same name."
        ),
        "schema": {
            "type": "object",
            "required": ["table_name"],
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": (
                        "Target TMDL table. Must already exist (use "
                        "create_table first)."
                    ),
                },
                "partition_name": {
                    "type": "string",
                    "description": ("Partition display name. Defaults to table_name."),
                },
                "mode": {
                    "type": "string",
                    "enum": ["import", "directQuery", "dual", "push"],
                    "description": "Storage mode (default: import).",
                },
                "template": {
                    "type": "string",
                    "enum": [
                        "csv",
                        "sql",
                        "json",
                        "sharepoint",
                        "odata",
                        "web",
                    ],
                    "description": (
                        "Template name. Mutually exclusive with " "'m_expression'."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Template-specific parameters. csv: {path, "
                        "delimiter, encoding, has_headers}. sql: "
                        "{server, database, query, privacy}. json: "
                        "{path}. sharepoint: {site_url, file_path}. "
                        "odata: {url}. web: {url}."
                    ),
                },
                "m_expression": {
                    "type": "string",
                    "description": (
                        "Raw M query string. Mutually exclusive with "
                        "'template'. The handler validates shape only; "
                        "no eval."
                    ),
                },
                "promote": {
                    "type": "boolean",
                    "description": (
                        "If true, wrap the staging query in "
                        "Table.PromoteHeaders + Table.TransformColumnTypes."
                    ),
                },
                "column_types": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "type"],
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                        },
                    },
                    "description": (
                        "Optional column type overrides, applied after "
                        "PromoteHeaders."
                    ),
                },
                "replace": {
                    "type": "boolean",
                    "description": (
                        "Overwrite an existing partition of the same "
                        "name on this table."
                    ),
                },
            },
            "anyOf": [
                {"required": ["template", "params"]},
                {"required": ["m_expression"]},
            ],
        },
        "handler": add_power_query_partition_handler,
    },
    {
        "name": "set_page_layout",
        "description": "Adjust PBIR page size, theme tokens, and background.",
        "schema": {
            "type": "object",
            "required": ["page"],
            "properties": {
                "page": {"type": "string"},
                "size": {"type": "object"},
                "theme_tokens": {"type": "object"},
                "background": {"type": "object"},
            },
        },
        "handler": set_page_layout_handler,
    },
    {
        "name": "add_rls_role",
        "description": "Create a row-level security role with dynamic or static table filters.",
        "schema": {
            "type": "object",
            "required": ["role_name", "table_permissions"],
            "properties": {
                "role_name": {"type": "string"},
                "model_permission": {"type": "string"},
                "table_permissions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["table_name", "filter_expression"],
                        "properties": {
                            "table_name": {"type": "string"},
                            "filter_expression": {"type": "string"},
                        },
                    },
                },
            },
        },
        "handler": add_rls_role_handler,
    },
    {
        "name": "add_ols_role",
        "description": (
            "Create an object-level security (OLS) role to hide tables or "
            "columns. Power BI Desktop emits nested tablePermission / "
            "columnPermission blocks per the Sept 2025 TMDL spec. "
            "metadata_permission is 'none' (hide from role) or 'read' "
            "(allow; default in Power BI Desktop)."
        ),
        "schema": {
            "type": "object",
            "required": ["role_name"],
            "properties": {
                "role_name": {"type": "string"},
                "model_permission": {
                    "type": "string",
                    "description": (
                        "Top-level modelPermission. Power BI defaults "
                        "to 'read' when omitted."
                    ),
                },
                "table_permissions": {
                    "type": "array",
                    "description": (
                        "OLS rules at table level. Each entry is "
                        "{table_name, metadata_permission} where "
                        "metadata_permission is 'none' or 'read'."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["table_name"],
                        "properties": {
                            "table_name": {"type": "string"},
                            "metadata_permission": {
                                "type": "string",
                                "enum": ["none", "read"],
                            },
                        },
                    },
                },
                "column_permissions": {
                    "type": "array",
                    "description": (
                        "OLS rules at column level. Each entry is "
                        "{table_name, column_name, metadata_permission}."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["table_name", "column_name"],
                        "properties": {
                            "table_name": {"type": "string"},
                            "column_name": {"type": "string"},
                            "metadata_permission": {
                                "type": "string",
                                "enum": ["none", "read"],
                            },
                        },
                    },
                },
                "hidden_tables": {
                    "type": "array",
                    "description": (
                        "Legacy alias for table_permissions. Each "
                        "entry is a table name; emitted as metadataPermission = none."
                    ),
                    "items": {"type": "string"},
                },
                "hidden_columns": {
                    "type": "array",
                    "description": (
                        "Legacy alias for column_permissions with "
                        "metadataPermission = none."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["table_name", "column_name"],
                        "properties": {
                            "table_name": {"type": "string"},
                            "column_name": {"type": "string"},
                        },
                    },
                },
            },
        },
        "handler": add_ols_role_handler,
    },
    {
        "name": "add_visual",
        "description": "Drop a visual container on a PBIR page with bindings and filters.",
        "schema": {
            "type": "object",
            "required": ["page", "visual_type", "bindings"],
            "properties": {
                "page": {"type": "string"},
                "visual_type": {"type": "string"},
                "bindings": {"type": "object"},
                "position": {"type": "object"},
                "title": {"type": "string"},
                "filters": {"type": "array"},
            },
        },
        "handler": add_visual_handler,
    },
    {
        "name": "package_pbip",
        "description": "Materialize the final PBIP directory suitable for Git or Fabric.",
        "schema": {
            "type": "object",
            "required": ["output_path"],
            "properties": {
                "output_path": {"type": "string"},
                "project_name": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
        },
        "handler": package_pbip_handler,
    },
]


def register_builtin_tools(orchestrator: Orchestrator) -> None:
    """Register the nl2pbip toolchain on the provided orchestrator."""

    for spec in DEFAULT_TOOL_DEFINITIONS:
        orchestrator.register_tool(
            name=spec["name"],
            description=spec["description"],
            schema=spec["schema"],
            handler=spec["handler"],
        )


# ----------------------------------------------------------------------
# Utility helpers
# ----------------------------------------------------------------------
def _matches_type(expected: Any, value: Any) -> bool:
    """Tiny subset of JSON Schema type checking sufficient for tool payloads."""

    mapping = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "object": dict,
        "array": list,
    }

    if isinstance(expected, list):
        return any(_matches_type(entry, value) for entry in expected)

    py_type = mapping.get(expected)
    if py_type is None:
        return True  # fallback for enums/const

    if expected == "number" and isinstance(value, bool):
        return False
    if expected == "integer" and isinstance(value, bool):
        return False

    return isinstance(value, py_type)
