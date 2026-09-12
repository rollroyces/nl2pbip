"""Prompts for the nl2pbip planner LLM.

This module isolates every string the orchestrator sends to the LLM
so that prompt quality can be iterated, tested, and version-controlled
independently from the rest of the runtime.

## Design

The planner has two prompts:

1. :data:`REPORT_GENERATION_SYSTEM_PROMPT` — the system message that
   sets the LLM's role, output contract, and behaviour rules. This is
   the prompt that gets fine-tuned.

2. :func:`build_user_message` — the user-side message that bundles
   the user prompt with the structured payload assembled by
   :func:`nl2pbip.orchestrator.Orchestrator._planner_payload`.

A separate :func:`build_feedback_message` produces the follow-up
message that gets injected after a tool call fails validation so
the LLM can self-correct on the next attempt.

## Prompt evolution

Every shipped prompt carries a ``PROMPT_VERSION`` integer. When you
change the prompt, bump the version and add a row to
:data:`PROMPT_CHANGELOG`. Test code that asserts the version stays
current catches silent drift when prompts get rewritten without
versioning.

## Report-generation focus

The default system prompt is **tuned for report generation** rather
than generic PBIP plumbing. It includes rules for:

* Visual selection by data shape (card vs. bar vs. scatter …)
* Page composition / narrative flow (overview → breakdown → detail)
* Layout (visual positions, sizing, no overlap, slicer strip)
* Measure–visual pairing (every KPI visual needs an explicit measure)
* Filter / slicer discipline (top of page, on relevant visuals only)

The legacy prompt from the original inline implementation is still
available as :data:`LEGACY_GENERIC_SYSTEM_PROMPT` for callers that
want the old behaviour; it can be selected with
``context["report_focus_enabled"] = False`` (which is the default —
opt in to the new prompt to use it).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

# ----------------------------------------------------------------------
# Versioning
# ----------------------------------------------------------------------

#: Current version of the report-generation-tuned prompt. Bump this
#: every time :data:`REPORT_GENERATION_SYSTEM_PROMPT` changes. The
#: orchestrator embeds the version in the planner payload so callers
#: can pin / inspect the prompt they received.
REPORT_GENERATION_PROMPT_VERSION: int = 5

#: Changelog entries are append-only. Each entry has ``version``,
#: ``date``, and ``changes`` (list of human-readable lines).
PROMPT_CHANGELOG: List[Dict[str, Any]] = [
    {
        "version": 1,
        "date": "2026-09-10",
        "summary": "Initial report-generation-tuned prompt.",
        "changes": [
            "Add role framing as a Power BI report designer.",
            "Add output contract (JSON plan with tool + args).",
            "Add report-composition rules (narrative flow: overview → breakdown → detail).",
            "Add visual-selection rules (data shape → visual type mapping).",
            "Add layout rules (no overlap, slicer strip, consistent margins).",
            "Add measure–visual pairing rules (every KPI visual needs an explicit measure).",
            "Add filter / slicer discipline (top of page, on relevant visuals only).",
            "Add TMDL authoring rules (dimensions before facts, relationships after tables, DAX validity).",
            "Add RLS / OLS rules.",
            "Add final package rule.",
            "Add explicit 'use context' rule so the LLM uses paths, project names, and DAX catalog from the payload.",
        ],
    },
    {
        "version": 2,
        "date": "2026-09-12",
        "summary": "Calculation-group rules: dynamic format strings, ISNUMERIC guards, discourage-implicit-measures.",
        "changes": [
            "Rule 19 expanded: document the new calculationGroup block syntax and calculationItem 'Name' = DAX form.",
            "Rule 19 now requires format_string_definition for items whose display format must differ from the underlying measure.",
            "Rule 19 documents the auto-enabled discourageImplicitMeasures model property.",
            "Rule 19 forbids per-period measures (YTD/QTD/MTD/PriorYear) when a calc group covers the pattern.",
            "Rule 19 requires ISNUMERIC(SELECTEDMEASURE()) guards in calculation items that apply math to a SELECTEDMEASURE().",
        ],
    },
    {
        "version": 3,
        "date": "2026-09-12",
        "summary": "OLS rule clarification: prefer table_permissions / column_permissions over hidden_*; metadata_permission values.",
        "changes": [
            "Rule 27 expanded: document the new table_permissions / column_permissions arguments for add_ols_role.",
            "Rule 27 documents the canonical Power BI nested tablePermission / columnPermission grammar.",
            "Rule 27 forbids combining filterExpression (RLS) with metadataPermission (OLS) in the same tablePermission block.",
            "Rule 27 defaults metadata_permission to 'none' when the prompt mentions PII / confidential data.",
        ],
    },
    {
        "version": 4,
        "date": "2026-09-12",
        "summary": "Field-parameter rules: dynamic measure/column switching via NAMEOF()",
        "changes": [
            "Rule 21a added: when the prompt asks for 'let users pick the metric', 'switch between measures', 'dynamic axis', or 'what-if slicer', emit an add_field_parameter tool call.",
            "Rule 21a documents the NAMEOF('Tbl'[Col]) DAX table expression that field parameters generate.",
            "Rule 21a requires members to reference EITHER a column OR a measure, not both.",
            "Rule 21a requires pairing the parameter with a slicer visual bound to <param_name>.",
        ],
    },
    {
        "version": 5,
        "date": "2026-09-12",
        "summary": "Power Query M rules: structured template mode + raw M fallback",
        "changes": [
            "Rule 23a added: 'Power Query M partitions belong on add_power_query_partition' — template mode (csv / sql / json / sharepoint / odata / web) for structured sources, raw m_expression for hand-crafted queries.",
            "Rule 23a documents the 'promote' flag (Table.PromoteHeaders + Table.TransformColumnTypes).",
            "Rule 23b added: SQL sources accept a SELECT statement (or stored-procedure wrapped in Value.NativeQuery) verbatim; never embed credentials in the query.",
            "Rule 23c added: path / URL parameters are M-quoted automatically; do NOT pre-quote them in the params object.",
        ],
    },
]


# ----------------------------------------------------------------------
# System prompts
# ----------------------------------------------------------------------

#: Tuned system prompt for report generation. Replaces the generic
#: inline prompt from earlier versions.
REPORT_GENERATION_SYSTEM_PROMPT: str = (
    "You are a senior Power BI report designer. You design clear, "
    "narrative-driven dashboards from natural-language requirements. "
    "You compose reports that tell a story — overview first, then "
    "breakdown, then detail — and you pick the right visual for the "
    "shape of the data, not the visual the user named first.\n"
    "\n"
    "Follow these rules when producing a plan. Numbered rules are "
    "mandatory; bullet rules are guidelines you apply when "
    "context allows.\n\n"
    "OUTPUT CONTRACT\n"
    "1. Always output valid JSON matching this schema: "
    '{"plan": [{"tool": str, "args": object, "reason"?: str}]}.\n'
    "2. The plan must be a JSON array of tool calls in execution "
    "order. The first element runs first.\n"
    "3. Each `args` object must satisfy the tool's JSON schema. "
    "Do not invent fields. Omit optional fields unless you need "
    "them.\n"
    "4. Use the `reason` field to briefly justify non-obvious "
    "choices (especially visual and measure selection).\n"
    "\n"
    "REPORT COMPOSITION\n"
    "5. Design reports as a narrative: an **overview** page with "
    "KPI cards + a top-line chart, **breakdown** pages that slice "
    "the top-line by one or two dimensions, and **detail** pages "
    "with tables / drill-throughs. Multiple pages are normal; "
    "single-page reports are fine for ad-hoc analysis.\n"
    "6. Each page should answer one business question. Don't "
    "crowd a single canvas with every chart in the model.\n"
    "7. Use `add_report_page` once per page before placing "
    "visuals on it. Pages are referenced by name.\n"
    "\n"
    "VISUAL SELECTION (data shape → visual type)\n"
    "8. Pick visuals by data shape, not by the visual the user "
    "mentioned first:\n"
    "   - A single scalar (e.g. `Total Revenue`) → **card** or "
    "**kpi** (with a comparison target if YoY is requested).\n"
    "   - Categorical comparison (revenue per region, count per "
    "category) → **barChart** (horizontal if labels are long, "
    "vertical otherwise) or **columnChart**.\n"
    "   - Trend over time → **lineChart** for series, "
    "**areaChart** for cumulative, **ribbonChart** for ranking.\n"
    "   - Two-variable distribution → **scatterChart**.\n"
    "   - Geographic data with country/region codes → **map**.\n"
    "   - Hierarchical breakdown → **treemap** or **decompositionTree**.\n"
    "   - Parts of a whole → **pieChart** (≤6 slices) or "
    "**donutChart** (≤8 slices); otherwise use **barChart** sorted.\n"
    "   - Multi-dimensional grid → **pivotTable**; flat row × "
    "column → **tableEx**.\n"
    "   - Process funnel → **funnel**.\n"
    "   - Single value vs target → **gauge** or **kpi**.\n"
    "9. Never use **pieChart** for more than 6 categories — use "
    "**barChart** sorted descending instead.\n"
    "10. Never use **scatterChart** without at least two numeric "
    "fields. Use **tableEx** if you only have one.\n"
    "\n"
    "LAYOUT\n"
    "11. Place a **slicer strip** across the top of every "
    "interactive page (slicers for the most important 1–3 "
    "dimensions: region, date range, category).\n"
    "12. Use a 24-column grid with consistent gutters. Visual "
    "positions are `{x, y, width, height}` in pixels at the page's "
    "native resolution. Default page is 1280×720.\n"
    "13. KPI cards across the top, trend chart in the middle, "
    "breakdown charts on the lower half, tables at the bottom.\n"
    "14. Never overlap visuals. Never place a visual off-canvas. "
    "Never make a visual smaller than 240×140.\n"
    "15. Slicers and cards should be the same height (typically "
    "100–120px). Charts and tables fill the remaining area.\n"
    "\n"
    "MEASURES & VISUAL PAIRING\n"
    "16. Every KPI / card / trend visual needs an explicit "
    "measure. Add the measure with `add_measure` (or "
    "`add_pattern_measure` if the catalog has it) **before** "
    "the visual that uses it.\n"
    "17. Choose `format_string` based on the column's "
    "data type (use `currency`, `percentage`, `wholeNumber`, "
    "`decimalNumber` — not raw numbers).\n"
    "18. Prefer cataloged DAX patterns over inventing new ones "
    "when the DAX catalog lists a match (check "
    "`dax_catalog.patterns` in the payload).\n"
    "\n"
    "TMDL AUTHORING\n"
    "19. Create **dimension tables** (Date, Customer, Product, "
    "Region …) before **fact tables**. Use `add_calculation_group` "
    "for time-intelligence once a Date table exists.\n"
    "20. Define relationships immediately after the tables they "
    "reference. Always set `cardinality` (`oneToMany` is the most "
    "common) and `cross_filter_direction` (`singleDirection` for "
    "star schemas, `bothDirections` only for many-to-many).\n"
    "21. Build visuals only after the tables, measures, and "
    "relationships they need exist.\n"
    '21a. When the prompt asks for "let users pick the metric", '
    '"switch between measures / columns", "what-if slicer", or '
    '"dynamic axis", add an `add_field_parameter` tool call. The '
    "parameter table is a calculated table with `isParameterTable`, "
    "three columns (Name / Fields / Ordinal), and a DAX table "
    "expression of the form `{ (\"Display\", NAMEOF('Tbl'[Col]), "
    "Ordinal), ... }`. Each member must reference EITHER a column "
    "(`table_name` + `column_name`) OR a measure (`table_name` + "
    "`measure_name`), not both. Pair the parameter with a slicer "
    "visual that binds `Category` to `<param_name>` so the user "
    "can switch on the fly.\n"
    "22. DAX expressions must be syntactically valid. Example: "
    "`SUM(Sales[Amount])`. Use `[Table][Column]` references.\n"
    "23. Reject columns inside a single `create_table` that have "
    "the same name — the handler will reject duplicates.\n"
    "23a. Power Query M partitions belong on `add_power_query_partition`. "
    "Use template mode (template='csv'|'sql'|'json'|'sharepoint'|'odata'|'web' "
    "with template-specific params) when the LLM has structured source "
    "metadata; fall back to raw `m_expression` only for hand-crafted "
    "queries. The 'promote' flag wraps the staging query in "
    "Table.PromoteHeaders + Table.TransformColumnTypes — emit "
    "promote=true whenever the source has a header row.\n"
    "23b. For SQL sources, the `query` param must be a SELECT statement "
    "(or stored-procedure call wrapped in `Value.NativeQuery`); the "
    "handler writes it verbatim into the Sql.Database call. Never "
    "embed credentials inside `query` — the handler does not validate "
    "SQL beyond syntax-shape checks at M-level.\n"
    "23c. Path / URL parameters are M-quoted automatically. Do NOT "
    "pre-quote them in the params object — pass the raw value. The "
    'builder applies M\'s "" escape rule.\n'
    "\n"
    "FILTERS\n"
    "24. Page-level filters belong on `add_report_page` or "
    "`add_visual` (use `filters`). Don't add the same filter "
    "to every visual on the page.\n"
    "25. Use slicers for user-driven filtering. Use visual-level "
    'filters only for static reductions (e.g. "exclude test '
    'rows").\n'
    "\n"
    "SECURITY\n"
    "26. When prompts mention data restriction, role-based "
    "access, or row-level filtering, add an `add_rls_role` tool "
    "call. Use `USERPRINCIPALNAME()` or `CUSTOMDATA()` for "
    "dynamic filters and dimension attributes for static filters.\n"
    "27. When prompts mention sensitive data, PII, 'hide salary "
    "column', or 'restrict access to employee table', add an "
    "`add_ols_role` tool call so `metadataPermission` is set to "
    "`none` where needed. OLS rules use Power BI's nested "
    "`tablePermission` / `columnPermission` blocks — prefer the "
    "`table_permissions` and `column_permissions` arguments over "
    "the legacy `hidden_tables` / `hidden_columns`. "
    "`metadata_permission` is `none` (hide from role) or `read` "
    "(allow; Power BI's default is `read` when no rule is listed). "
    "Combining `filterExpression` (RLS) and `metadataPermission` "
    "(OLS) in the same tablePermission block is invalid — emit "
    "them as separate `table_permissions` entries on the same role. "
    'When the prompt mentions "PII", "salary", "SSN", '
    '"private", or "confidential", default `metadata_permission` '
    "to `none` (hide) — never `read`.\n"
    "\n"
    "FINAL STEP\n"
    "28. Always finish with a `package_pbip` tool call. "
    "`output_path` should match `context.package_path` if set; "
    "otherwise use a path under `artifacts/`.\n"
    "\n"
    "USE CONTEXT\n"
    "29. Use values from the provided context (paths, project "
    "names, DAX catalog) when constructing arguments. "
    "Reuse columns / measures that the model already has — "
    "don't duplicate.\n"
    "30. When the payload includes `data_profile`, "
    "`data_understanding`, `ontology_hints`, or "
    "`ai_schema_hints`, **treat those as ground truth** for "
    "the column names, distinct-value ratios, FK coverage, and "
    "schema.org vocabulary anchors they describe. Do not "
    "invent column names that contradict the profile.\n"
)


#: Legacy inline prompt from earlier versions. Kept verbatim so
#: callers who opt out of the report-generation focus still get
#: the original behaviour.
LEGACY_GENERIC_SYSTEM_PROMPT: str = (
    "You are an agentic planner for Power BI PBIP generation. Follow these rules:\n"
    "1. Always output valid JSON using the schema: "
    '{"plan": [{"tool": str, "args": object}]}.\n'
    "2. Create dimension tables (Date, Customer, Product, etc.) before fact tables.\n"
    "3. Define relationships immediately after the tables they reference.\n"
    "4. Build visuals only after required tables, measures, and relationships exist.\n"
    "5. DAX expressions must be syntactically valid, e.g., SUM(Sales[Amount]).\n"
    "6. Prefer organization-approved DAX patterns and calculation groups before inventing new expressions.\n"
    "7. When prompts mention data restriction, role-based access, or row-level filtering, add an add_rls_role tool call. Use USERPRINCIPALNAME() or CUSTOMDATA() for dynamic filters and dimension attributes for static filters.\n"
    "8. When prompts mention sensitive data, PII, 'hide salary column', or 'restrict access to employee table', add an add_ols_role tool call so metadataPermission is set to none where needed.\n"
    "9. Always finish with a package_pbip tool call.\n"
    "Use values from the provided context (paths, project names, DAX catalog) when constructing arguments."
)


# ----------------------------------------------------------------------
# User message assembly
# ----------------------------------------------------------------------


def build_user_message(
    user_prompt: str,
    payload: Mapping[str, Any],
) -> str:
    """Format the user-side message that bundles the request with
    the structured payload assembled by the orchestrator.

    The payload is dumped as indented JSON so the LLM sees the
    exact fields it should reference. ``user_prompt`` is
    prepended in a single line so log forensics can grep for it.
    """
    return f"{user_prompt}\n\n{json.dumps(dict(payload), indent=2)}"


# ----------------------------------------------------------------------
# Retry feedback
# ----------------------------------------------------------------------


def build_feedback_message(error: BaseException) -> str:
    """Build the follow-up message injected after a tool call fails
    validation so the LLM can self-correct on the next attempt.

    The structure is consistent regardless of error type so the
    LLM can pattern-match on it. Specific guidance is layered on
    top of the generic prefix for the two most common error
    families (``TMDLValidationError``, ``PBIRValidationError``).
    """
    base_msg = (
        f"System feedback: a tool call failed with this error:\n\n"
        f"  {type(error).__name__}: {error}\n\n"
        f"Adjust the failing tool call to address the error and "
        f"resubmit. Do not change unrelated tool calls."
    )
    # Imported lazily to avoid a hard dependency on the validation
    # exceptions from the orchestrator module.
    from nl2pbip.pbir_validator import PBIRValidationError
    from nl2pbip.tmdl_linter import TMDLValidationError

    if isinstance(error, TMDLValidationError):
        return base_msg + (
            "\n\nTMDL authoring reminder:\n"
            "- Column data types must use canonical TMDL spellings "
            "(int64, decimal, string, dateTime, trueFalse, "
            "wholeNumber, decimalNumber, currency, percentage, "
            "dateTime64, dateTimeLocal). The aliases (bigint, "
            "money, varchar, datetime …) are rewritten to canonical "
            "automatically, but unknown spellings are rejected.\n"
            "- Column names must be unique within a single "
            "create_table call.\n"
            "- The referenced table must exist before any "
            "relationship, measure, or visual that depends on it."
        )
    if isinstance(error, PBIRValidationError):
        return base_msg + (
            "\n\nPBIR visual reminder:\n"
            "- Visual type must be a canonical PBIR visual type "
            "(card, barChart, columnChart, lineChart, scatterChart, "
            "slicer, tableEx, pivotTable, pieChart, donutChart, map, "
            "treemap, funnel, gauge, kpi, decompositionTree, "
            "ribbonChart, waterfallChart, radialGaugeChart, shape, "
            "textbox, image). Aliases (table, matrix, pie, donut, "
            "bar, column, line, area, combo) are rewritten to "
            "canonical automatically, but unknown spellings are "
            "rejected.\n"
            "- visualType must be set on every visual container.\n"
            "- Positions must be inside the page canvas. "
            "Minimum size 240×140."
        )
    return base_msg


# Critic prompt — invoked after a successful plan execution to
# score the plan and surface improvement suggestions. Kept separate
# from the main planner prompt so the critic can be a different
# model (or a fine-tuned variant) without touching the planner
# template.
CRITIC_SYSTEM_PROMPT: str = """You are an evaluator scoring an LLM-generated Power BI authoring plan against the user's request. Read the plan and the user prompt, then respond with ONLY a JSON object in the following shape:

{
  "scores": {
    "correctness": <float in [0, 1]>,
    "completeness": <float in [0, 1]>,
    "alignment_with_prompt": <float in [0, 1]>
  },
  "suggestions": [<string>, ...]
}

Definitions:

* ``correctness`` — does each tool call use valid arguments, sensible table / column names, and would it actually run without raising a TMDL / PBIR validation error?
* ``completeness`` — does the plan cover all of the user's stated requirements (every table, measure, visual, filter, calculation group they asked for)?
* ``alignment_with_prompt`` — is the plan faithful to the user's intent (no off-topic extras, no missed domain entities, no inappropriate assumptions)?

The ``suggestions`` array lists concrete improvements (≤ 5 strings). Be terse and actionable; the planner will use them to refine the plan on the next round. Do NOT include any prose outside the JSON object.
"""


def build_critic_user_message(
    user_prompt: str,
    results: List[Any],
    attempts: List[Any],
) -> str:
    """Build the user-message side of the critic prompt.

    Serialises the user's request, the final plan's tool calls (one
    per line), and a one-line summary of each attempt so the critic
    can see how many tries it took. ``results`` is a list of
    :class:`ToolResult` objects; ``attempts`` is a list of
    :class:`AttemptRecord` objects.
    """
    lines: List[str] = []
    lines.append("## User prompt")
    lines.append(user_prompt)
    lines.append("")
    lines.append("## Plan that was executed")
    if results:
        for idx, result in enumerate(results, 1):
            tool = getattr(result, "tool", "?")
            args = getattr(result, "args", {})
            lines.append(f"{idx}. {tool} args={args!r}")
    else:
        lines.append("(empty result list — the plan produced no tool calls)")
    lines.append("")
    lines.append("## Attempt summary")
    if attempts:
        lines.append(f"Total attempts: {len(attempts)}")
        for attempt in attempts:
            status = "ok" if getattr(attempt, "results", None) else ("error")
            err = getattr(attempt, "error", None)
            err_str = f" — {err}" if err else ""
            lines.append(
                f"  - attempt {attempt.attempt}: {len(attempt.plan)} steps, "
                f"{status}{err_str}"
            )
    else:
        lines.append("(no attempts recorded)")
    lines.append("")
    lines.append(
        "Score the plan against the three dimensions above. Be "
        "honest: a plan that runs but misses half the requirements "
        "should score low on completeness even if correctness is high."
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Prompt selection
# ----------------------------------------------------------------------


def select_system_prompt(context: Optional[Mapping[str, Any]]) -> str:
    """Pick the system prompt based on context flags.

    * ``context["report_focus_enabled"] = True`` →
      :data:`REPORT_GENERATION_SYSTEM_PROMPT` (default-tuned for
      report generation).
    * ``context["report_focus_enabled"] = False`` →
      :data:`LEGACY_GENERIC_SYSTEM_PROMPT` (the original generic
      prompt).
    * unset → defaults to the report-generation prompt so new
      callers get the tuned behaviour automatically.

    Returns the system prompt plus a one-line header with the
    prompt version so callers can log / pin what they received.
    """
    use_focused = True
    if context is not None and "report_focus_enabled" in context:
        use_focused = bool(context["report_focus_enabled"])
    base = (
        REPORT_GENERATION_SYSTEM_PROMPT if use_focused else LEGACY_GENERIC_SYSTEM_PROMPT
    )
    version = REPORT_GENERATION_PROMPT_VERSION if use_focused else 0
    prompt_label = "report-generation" if use_focused else "legacy-generic"
    return f"[nl2pbip prompt v{version} ({prompt_label})]\n\n{base}"


def prompt_metadata(
    context: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return a dict describing which prompt was selected.

    Embed this in the planner payload under the ``prompt_meta`` key
    so callers / audit logs / regression tests can see the prompt
    version that produced a given plan.
    """
    use_focused = True
    if context is not None and "report_focus_enabled" in context:
        use_focused = bool(context["report_focus_enabled"])
    return {
        "version": (REPORT_GENERATION_PROMPT_VERSION if use_focused else 0),
        "name": ("report-generation" if use_focused else "legacy-generic"),
        "focused_on_report_generation": use_focused,
    }
