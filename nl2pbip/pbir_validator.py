"""Validation helpers for PBIR page and visual JSON payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence, Tuple

from jsonschema import Draft7Validator
from jsonschema import ValidationError as JSONSchemaValidationError

from nl2pbip.visual_types import (
    CANONICAL_VISUAL_TYPES,
    VisualTypeError,
    normalize_visual_type,
)

if TYPE_CHECKING:  # pragma: no cover - used only for typing
    try:
        from nl2pbip.tmdl_engine import TMDLModel
    except ImportError:  # pragma: no cover - fallback for editable installs
        from tmdl_engine import TMDLModel  # type: ignore


PBIR_PAGE_SCHEMA_URI = "http://powerbi.com/product/schema#page"
PBIR_VISUAL_SCHEMA_URI = "http://powerbi.com/product/schema#visualContainer"
DEFAULT_CANVAS_SIZE: Tuple[float, float] = (1280.0, 720.0)


def _format_error_path(path: Sequence[Any]) -> Optional[str]:
    if not path:
        return None
    return "/".join(str(part) for part in path)


@dataclass
class PBIRValidationError(Exception):
    """Structured validation error raised for invalid PBIR payloads."""

    file_type: str  # "page" or "visualContainer"
    message: str
    path: Optional[str] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        base = f"{self.file_type}: {self.message}"
        if self.path:
            base += f" (path: {self.path})"
        return base


class PBIRValidator:
    """Validate PBIR JSON artifacts using schema + semantic checks."""

    # Alias for backwards compatibility — the canonical set is now
    # maintained in :mod:`nl2pbip.visual_types` along with alias
    # resolution and default layout hints.
    _SUPPORTED_VISUALS = CANONICAL_VISUAL_TYPES

    _PAGE_SCHEMA: Dict[str, Any] = {
        "$id": "https://powerbi.microsoft.com/schema/page.v1.json",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["$schema", "name", "displayName", "pageSize", "visualContainers"],
        "properties": {
            "$schema": {"type": "string"},
            "name": {"type": "string"},
            "displayName": {"type": "string"},
            "pageSize": {
                "type": "object",
                "required": ["width", "height"],
                "properties": {
                    "width": {"type": ["integer", "number"]},
                    "height": {"type": ["integer", "number"]},
                },
            },
            "background": {"type": ["object", "null"]},
            "themeTokens": {"type": ["object", "null"]},
            "visualContainers": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
        "additionalProperties": True,
    }

    _VISUAL_SCHEMA: Dict[str, Any] = {
        "$id": "https://powerbi.microsoft.com/schema/visualContainer.v1.json",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["$schema", "name", "visualType", "layout", "config"],
        "properties": {
            "$schema": {"type": "string"},
            "name": {"type": "string"},
            "visualType": {"type": "string"},
            "title": {"type": ["string", "null"]},
            "filters": {"type": "array"},
            "layout": {
                "type": "object",
                "required": ["x", "y", "width", "height", "z"],
                "properties": {
                    "x": {"type": ["number", "integer"]},
                    "y": {"type": ["number", "integer"]},
                    "width": {"type": ["number", "integer"]},
                    "height": {"type": ["number", "integer"]},
                    "z": {"type": "integer"},
                },
            },
            "config": {
                "type": "object",
                "properties": {
                    "singleVisual": {
                        "type": "object",
                        "required": ["visualType", "projections"],
                        "properties": {
                            "visualType": {"type": "string"},
                            "projections": {"type": "object"},
                        },
                    }
                },
            },
        },
        "additionalProperties": True,
    }

    def __init__(
        self,
        model: Optional["TMDLModel"] = None,
        canvas_size: Tuple[float, float] = DEFAULT_CANVAS_SIZE,
    ) -> None:
        self._model = model
        self._default_canvas = canvas_size
        self._column_lookup = self._build_column_lookup(model) if model else {}
        self._measure_set = self._build_measure_set(model) if model else set()
        self._page_validator = Draft7Validator(self._PAGE_SCHEMA)
        self._visual_validator = Draft7Validator(self._VISUAL_SCHEMA)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def validate_page(self, page_json: Dict[str, Any]) -> None:
        self._require_schema_uri(page_json, PBIR_PAGE_SCHEMA_URI, "page")
        self._run_validator(self._page_validator, page_json, "page")
        page_size = page_json["pageSize"]
        width = self._require_number(
            page_size.get("width"),
            "pageSize.width",
            allow_zero=False,
            file_type="page",
        )
        height = self._require_number(
            page_size.get("height"),
            "pageSize.height",
            allow_zero=False,
            file_type="page",
        )
        visuals = page_json.get("visualContainers", []) or []
        for idx, visual in enumerate(visuals):
            try:
                self.validate_visual(visual, page_bounds=(width, height))
            except PBIRValidationError as exc:
                nested_path = f"visualContainers/{idx}"
                if exc.path:
                    nested_path = f"{nested_path}/{exc.path}"
                raise PBIRValidationError(
                    exc.file_type, exc.message, path=nested_path
                ) from exc

    def validate_visual(
        self,
        visual_json: Dict[str, Any],
        page_bounds: Optional[Tuple[float, float]] = None,
    ) -> None:
        # Check visualType presence BEFORE running the JSON schema
        # validator so a missing field surfaces a clearer message
        # than the jsonschema "required property" boilerplate.
        visual_type = visual_json.get("visualType")
        if not isinstance(visual_type, str) or not visual_type:
            raise PBIRValidationError(
                "visualContainer",
                "visualType is required and must be a non-empty string.",
                path="visualType",
            )
        self._require_schema_uri(visual_json, PBIR_VISUAL_SCHEMA_URI, "visualContainer")
        self._run_validator(self._visual_validator, visual_json, "visualContainer")
        layout = visual_json["layout"]
        x = self._require_number(
            layout.get("x"), "layout.x", allow_zero=True, file_type="visualContainer"
        )
        y = self._require_number(
            layout.get("y"), "layout.y", allow_zero=True, file_type="visualContainer"
        )
        width = self._require_number(
            layout.get("width"),
            "layout.width",
            allow_zero=False,
            file_type="visualContainer",
        )
        height = self._require_number(
            layout.get("height"),
            "layout.height",
            allow_zero=False,
            file_type="visualContainer",
        )
        z = layout.get("z")
        if isinstance(z, bool) or not isinstance(z, int):
            raise PBIRValidationError(
                "visualContainer", "layout.z must be an integer.", path="layout.z"
            )
        bounds = page_bounds or self._default_canvas
        canvas_w, canvas_h = bounds
        if x < 0 or y < 0:
            raise PBIRValidationError(
                "visualContainer", "layout coordinates must be non-negative."
            )
        if x + width > canvas_w or y + height > canvas_h:
            raise PBIRValidationError(
                "visualContainer",
                f"Visual layout exceeds canvas dimensions ({int(canvas_w)}x{int(canvas_h)}).",
            )
        visual_type = visual_json.get("visualType")
        if not isinstance(visual_type, str) or not visual_type:
            raise PBIRValidationError(
                "visualContainer",
                "visualType is required and must be a non-empty string.",
                path="visualType",
            )
        # Accept both the canonical spelling and any friendly alias
        # (``table``, ``matrix``, ``pie`` …). We normalise first so the
        # on-disk JSON always carries the canonical spelling Power BI
        # Desktop expects. ``normalize_visual_type`` raises
        # ``VisualTypeError`` for unrecognised inputs; we surface that
        # as a ``PBIRValidationError`` so the orchestrator's retry
        # loop sees a single error type from this validator.
        try:
            canonical_type = normalize_visual_type(visual_type)
        except VisualTypeError as exc:
            raise PBIRValidationError(
                "visualContainer",
                f"Unsupported visualType {visual_type!r}. {exc}",
                path="visualType",
            ) from exc
        if canonical_type != visual_type:
            visual_json["visualType"] = canonical_type
        if canonical_type not in CANONICAL_VISUAL_TYPES:
            # Defensive: normalize_visual_type should have raised
            # already if the type isn't canonical, but a future
            # change to the alias map might let something through.
            raise PBIRValidationError(
                "visualContainer",
                f"Unsupported visualType {visual_type!r}. "
                f"Canonical types: {sorted(CANONICAL_VISUAL_TYPES)}.",
                path="visualType",
            )
        single_visual = visual_json.get("config", {}).get("singleVisual", {})
        projections = single_visual.get("projections")
        if projections is None:
            raise PBIRValidationError(
                "visualContainer", "singleVisual.projections is required."
            )
        inner_type = single_visual.get("visualType")
        if inner_type:
            # Also normalise the inner visualType so a caller who
            # supplied the alias in both places ends up with both
            # spellings canonicalised on disk.
            canonical_inner = normalize_visual_type(inner_type)
            if canonical_inner != inner_type:
                single_visual["visualType"] = canonical_inner
                inner_type = canonical_inner
            if inner_type != canonical_type:
                raise PBIRValidationError(
                    "visualContainer",
                    "config.singleVisual.visualType must match visualType.",
                    path="config/singleVisual/visualType",
                )
        self._validate_projections(projections)

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------
    def _run_validator(
        self,
        validator: Draft7Validator,
        payload: Dict[str, Any],
        file_type: str,
    ) -> None:
        try:
            validator.validate(payload)
        except (
            JSONSchemaValidationError
        ) as exc:  # pragma: no cover - jsonschema already tested
            raise PBIRValidationError(
                file_type, exc.message, path=_format_error_path(exc.absolute_path)
            ) from exc

    def _require_schema_uri(
        self, payload: Dict[str, Any], expected: str, file_type: str
    ) -> None:
        uri = payload.get("$schema")
        if not uri:
            raise PBIRValidationError(
                file_type, f"$schema must be '{expected}'.", path="$schema"
            )
        if uri != expected:
            raise PBIRValidationError(
                file_type,
                f"$schema must be '{expected}' (got '{uri}').",
                path="$schema",
            )

    def _require_number(
        self,
        value: Any,
        path: str,
        *,
        allow_zero: bool,
        file_type: str,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PBIRValidationError(file_type, f"{path} must be numeric.", path=path)
        if value < 0:
            raise PBIRValidationError(
                file_type, f"{path} must be non-negative.", path=path
            )
        if not allow_zero and value == 0:
            raise PBIRValidationError(
                file_type, f"{path} must be greater than zero.", path=path
            )
        return float(value)

    # ------------------------------------------------------------------
    # Projection + schema helpers
    # ------------------------------------------------------------------
    def _validate_projections(self, projections: Dict[str, Any]) -> None:
        if not isinstance(projections, dict) or not projections:
            raise PBIRValidationError(
                "visualContainer", "singleVisual.projections must contain bindings."
            )
        for role, entries in projections.items():
            if not isinstance(entries, list) or not entries:
                raise PBIRValidationError(
                    "visualContainer",
                    f"Projection '{role}' must contain an array of role bindings.",
                )
            for entry in entries:
                if not isinstance(entry, dict):
                    raise PBIRValidationError(
                        "visualContainer",
                        f"Projection '{role}' contains an invalid binding entry.",
                    )
                query_ref = entry.get("queryRef")
                measure_ref = entry.get("measureRef")
                if query_ref:
                    self._validate_query_ref(role, query_ref)
                elif measure_ref:
                    self._validate_measure_ref(role, measure_ref)
                else:
                    raise PBIRValidationError(
                        "visualContainer",
                        f"Projection '{role}' must declare a queryRef or measureRef.",
                    )

    def _validate_query_ref(self, role: str, query_ref: str) -> None:
        if "[" not in query_ref or not query_ref.endswith("]"):
            raise PBIRValidationError(
                "visualContainer",
                f"Projection '{role}' has invalid queryRef '{query_ref}'.",
            )
        table_part, column_part = query_ref.split("[", 1)
        table = table_part.strip()
        column = column_part[:-1].strip()
        if not table or not column:
            raise PBIRValidationError(
                "visualContainer",
                f"Projection '{role}' has malformed queryRef '{query_ref}'.",
            )
        if not self._model:
            return
        columns = self._column_lookup.get(table)
        if not columns or column not in columns:
            raise PBIRValidationError(
                "visualContainer",
                f"Projection '{role}' references unknown field '{query_ref}'.",
            )

    def _validate_measure_ref(self, role: str, measure_ref: str) -> None:
        if not isinstance(measure_ref, str) or not measure_ref.strip():
            raise PBIRValidationError(
                "visualContainer",
                f"Projection '{role}' has invalid measureRef '{measure_ref}'.",
            )
        if not self._model:
            return
        if measure_ref not in self._measure_set:
            raise PBIRValidationError(
                "visualContainer",
                f"Projection '{role}' references unknown measure '{measure_ref}'.",
            )

    def _build_column_lookup(self, model: "TMDLModel") -> Dict[str, set]:
        lookup: Dict[str, set] = {}
        for name, table in getattr(model, "tables", {}).items():
            columns = getattr(table, "columns", {}) or {}
            lookup[name] = set(columns.keys())
        return lookup

    def _build_measure_set(self, model: "TMDLModel") -> set:
        measures = set()
        for table in getattr(model, "tables", {}).values():
            for measure_name in getattr(table, "measures", {}).keys():
                measures.add(measure_name)
        return measures
