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

from dax_catalog import DAXCatalog
from packager import package_pbip_handler
from pbir_engine import add_report_page_handler, add_visual_handler, set_page_layout_handler
from pbir_validator import PBIRValidationError
from tmdl_engine import (
    add_calculation_group_handler,
    add_measure_handler,
    add_pattern_measure_handler,
    add_ols_role_handler,
    add_rls_role_handler,
    create_table_handler,
    define_relationship_handler,
)
from tmdl_linter import TMDLValidationError


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
        """Shallow JSON Schema-ish validation to guard obvious mistakes."""

        required = self.schema.get("required", [])
        properties = self.schema.get("properties", {})

        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(
                f"Tool '{self.name}' missing required fields: {', '.join(missing)}"
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

    def run(self, user_prompt: str, context: Optional[Dict[str, Any]] = None) -> List[ToolResult]:
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
            except (TMDLValidationError, PBIRValidationError, ValueError, TypeError) as exc:
                last_error = exc
                feedback.append(self._feedback_for_exception(exc))
                if attempt == max_attempts:
                    break
        if last_error is not None:
            raise last_error
        raise RuntimeError("Planner retries exceeded without validation detail.")

    # ------------------------------------------------------------------
    # Planning helpers
    # ------------------------------------------------------------------
    def _request_plan(self, user_prompt: str, context: Dict[str, Any]) -> str:
        planning_messages = [
            {
                "role": "system",
                "content": (
                    "You are an agentic planner for Power BI PBIP generation. Follow these rules:\n"
                    "1. Always output valid JSON using the schema: {\"plan\": [{\"tool\": str, \"args\": object}]}.\n"
                    "2. Create dimension tables (Date, Customer, Product, etc.) before fact tables.\n"
                    "3. Define relationships immediately after the tables they reference.\n"
                    "4. Build visuals only after required tables, measures, and relationships exist.\n"
                    "5. DAX expressions must be syntactically valid, e.g., SUM(Sales[Amount]).\n"
                    "6. Prefer organization-approved DAX patterns and calculation groups before inventing new expressions.\n"
                    "7. When prompts mention data restriction, role-based access, or row-level filtering, add an add_rls_role tool call. Use USERPRINCIPALNAME() or CUSTOMDATA() for dynamic filters and dimension attributes for static filters.\n"
                    "8. When prompts mention sensitive data, PII, 'hide salary column', or 'restrict access to employee table', add an add_ols_role tool call so metadataPermission is set to none where needed.\n"
                    "9. Always finish with a package_pbip tool call.\n"
                    "Use values from the provided context (paths, project names, DAX catalog) when constructing arguments."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(self._planner_payload(user_prompt, context), indent=2),
            },
        ]
        return self._llm.generate(planning_messages)

    def _parse_plan(self, plan_text: str) -> List[ToolCall]:
        try:
            raw_plan = json.loads(plan_text)
        except json.JSONDecodeError as exc:
            raise ValueError("Planner must return valid JSON.") from exc

        if isinstance(raw_plan, dict) and "plan" in raw_plan:
            raw_plan = raw_plan["plan"]

        if not isinstance(raw_plan, list):
            raise ValueError("Planner output must be a JSON array of tool calls or an object with a 'plan' array.")

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

    def _planner_payload(self, user_prompt: str, context: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "prompt": user_prompt,
            "context": context,
            "tools": [self._tool_stub(spec) for spec in self._tools.all_specs()],
        }
        if self._dax_catalog:
            payload["dax_catalog"] = self._dax_catalog.prompt_payload()
        return payload

    def _augment_prompt(self, base_prompt: str, feedback: List[str]) -> str:
        if not feedback:
            return base_prompt
        return f"{base_prompt}\n\n{feedback[-1]}"

    def _feedback_for_exception(self, error: Exception) -> str:
        if isinstance(error, TMDLValidationError):
            return (
                "System feedback: The generated TMDL failed validation with error: "
                f"{error}. Please correct the syntax and retry the tool call."
            )
        if isinstance(error, PBIRValidationError):
            return (
                "System feedback: The generated PBIR visual JSON failed validation with error: "
                f"{error}. Please adjust the visual properties/coordinates and retry the tool call."
            )
        return f"System feedback: {error}"  # pragma: no cover - fallback path


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
        "description": "Add a predefined calculation group table from the DAX catalog.",
        "schema": {
            "type": "object",
            "required": ["group_key"],
            "properties": {
                "group_key": {"type": "string"},
                "table_name": {"type": "string"},
                "precedence": {"type": "integer"},
            },
        },
        "handler": add_calculation_group_handler,
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
        "description": "Create an object-level security role to hide tables or columns via metadataPermission: none.",
        "schema": {
            "type": "object",
            "required": ["role_name"],
            "properties": {
                "role_name": {"type": "string"},
                "model_permission": {"type": "string"},
                "hidden_tables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "hidden_columns": {
                    "type": "array",
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
