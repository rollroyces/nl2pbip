"""PBIR generation, layout, and tool handlers for nl2pbip."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # Support both package and local execution
    from nl2pbip.pbir_validator import PBIRValidator, PBIR_PAGE_SCHEMA_URI, PBIR_VISUAL_SCHEMA_URI
except ImportError:  # pragma: no cover - fallback for editable installs
    from pbir_validator import PBIRValidator, PBIR_PAGE_SCHEMA_URI, PBIR_VISUAL_SCHEMA_URI  # type: ignore

try:
    from nl2pbip.tmdl_engine import MODEL_PATH_KEY, TMDLModel, load_model
except ImportError:  # pragma: no cover - fallback for editable installs
    from tmdl_engine import MODEL_PATH_KEY, TMDLModel, load_model  # type: ignore


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class PageSize:
    width: int = 1280
    height: int = 720

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PageSize":
        return cls(width=payload.get("width", 1280), height=payload.get("height", 720))

    def to_dict(self) -> Dict[str, Any]:
        return {"width": self.width, "height": self.height}


@dataclass
class VisualPosition:
    x: float
    y: float
    width: float
    height: float
    z: int

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "VisualPosition":
        return cls(
            x=payload.get("x", 0),
            y=payload.get("y", 0),
            width=payload.get("width", 200),
            height=payload.get("height", 200),
            z=payload.get("z", 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "z": self.z,
        }


@dataclass
class BindingEntry:
    """Represents a single binding expression and how PBIR should serialize it."""

    value: str  # Canonical projection target (measure name or queryRef string)
    kind: str  # "measure" or "column"
    raw: str

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "kind": self.kind, "raw": self.raw}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BindingEntry":
        value = payload.get("value") or payload.get("raw") or ""
        kind = payload.get("kind", "column")
        raw = payload.get("raw", value)
        return cls(value=value, kind=kind, raw=raw)


@dataclass
class VisualBinding:
    role: str
    entries: List[BindingEntry]

    @classmethod
    def from_mapping(
        cls,
        mapping: Dict[str, List[str]],
        model: Optional[TMDLModel],
    ) -> List["VisualBinding"]:
        bindings: List[VisualBinding] = []
        for role, exprs in mapping.items():
            entries = [_classify_binding_entry(expr, model) for expr in exprs]
            bindings.append(cls(role=role, entries=entries))
        return bindings

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "VisualBinding":
        entries_payload = payload.get("entries")
        if entries_payload:
            entries = [BindingEntry.from_dict(item) for item in entries_payload]
        else:
            legacy_exprs = payload.get("expressions", [])
            entries = [_legacy_binding_entry(expr) for expr in legacy_exprs]
        return cls(role=payload["role"], entries=entries)

    @classmethod
    def from_projections(cls, projections: Dict[str, List[Dict[str, Any]]]) -> List["VisualBinding"]:
        bindings: List[VisualBinding] = []
        for role, entries in (projections or {}).items():
            binding_entries: List[BindingEntry] = []
            for entry in entries:
                if "measureRef" in entry:
                    value = entry["measureRef"]
                    binding_entries.append(BindingEntry(value=value, kind="measure", raw=value))
                elif "queryRef" in entry:
                    value = entry["queryRef"]
                    binding_entries.append(BindingEntry(value=value, kind="column", raw=value))
                else:
                    binding_entries.append(BindingEntry(value="", kind="column", raw=""))
            bindings.append(cls(role=role, entries=binding_entries))
        return bindings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass
class VisualContainer:
    visual_id: str
    visual_type: str
    title: Optional[str]
    bindings: List[VisualBinding]
    filters: List[str]
    position: VisualPosition

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "VisualContainer":
        bindings = [VisualBinding.from_dict(item) for item in payload.get("bindings", [])]
        return cls(
            visual_id=payload["visual_id"],
            visual_type=payload["visual_type"],
            title=payload.get("title"),
            bindings=bindings,
            filters=payload.get("filters", []),
            position=VisualPosition.from_dict(payload.get("position", {})),
        )

    @classmethod
    def from_visual_json(cls, payload: Dict[str, Any]) -> "VisualContainer":
        config = payload.get("config", {})
        single_visual = config.get("singleVisual", {})
        projections = single_visual.get("projections", {})
        bindings = VisualBinding.from_projections(projections)
        visual_id = payload.get("name") or payload.get("visual_id") or f"visual_{uuid.uuid4().hex[:8]}"
        visual_type = payload.get("visualType") or single_visual.get("visualType", "tableEx")
        return cls(
            visual_id=visual_id,
            visual_type=visual_type,
            title=payload.get("title"),
            bindings=bindings,
            filters=payload.get("filters", []),
            position=VisualPosition.from_dict(payload.get("layout", {})),
        )

    def to_visual_json(self) -> Dict[str, Any]:
        projections: Dict[str, List[Dict[str, Any]]] = {}
        for binding in self.bindings:
            role_entries: List[Dict[str, Any]] = []
            for entry in binding.entries:
                if entry.kind == "measure":
                    role_entries.append({"measureRef": entry.value})
                else:
                    role_entries.append({"queryRef": entry.value})
            projections[binding.role] = role_entries
        return {
            "$schema": PBIR_VISUAL_SCHEMA_URI,
            "name": self.visual_id,
            "visualType": self.visual_type,
            "title": self.title,
            "layout": self.position.to_dict(),
            "filters": self.filters,
            "config": {
                "singleVisual": {
                    "visualType": self.visual_type,
                    "projections": projections,
                }
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "visual_id": self.visual_id,
            "visual_type": self.visual_type,
            "title": self.title,
            "bindings": [binding.to_dict() for binding in self.bindings],
            "filters": self.filters,
            "position": self.position.to_dict(),
        }


@dataclass
class ReportPage:
    name: str
    display_name: str
    size: PageSize = field(default_factory=PageSize)
    theme_tokens: Dict[str, Any] = field(default_factory=dict)
    background: Optional[Dict[str, Any]] = None
    visuals: List[VisualContainer] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ReportPage":
        display_name = payload.get("display_name") or payload.get("displayName") or payload["name"]
        size_payload = payload.get("size")
        if size_payload is None:
            size_payload = payload.get("pageSize", {})
        theme_payload = payload.get("theme_tokens")
        if theme_payload is None:
            theme_payload = payload.get("themeTokens", {})
        visuals_payload = payload.get("visuals")
        if visuals_payload:
            visuals = [VisualContainer.from_dict(item) for item in visuals_payload]
        elif "visualContainers" in payload:
            visuals = [VisualContainer.from_visual_json(item) for item in payload.get("visualContainers", [])]
        else:
            visuals = []
        return cls(
            name=payload["name"],
            display_name=display_name,
            size=PageSize.from_dict(size_payload),
            theme_tokens=theme_payload,
            background=payload.get("background"),
            visuals=visuals,
        )

    def to_page_json(self) -> Dict[str, Any]:
        return {
            "$schema": PBIR_PAGE_SCHEMA_URI,
            "name": self.name,
            "displayName": self.display_name,
            "pageSize": self.size.to_dict(),
            "background": self.background,
            "themeTokens": self.theme_tokens,
            "visualContainers": [visual.to_visual_json() for visual in self.visuals],
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "size": self.size.to_dict(),
            "theme_tokens": self.theme_tokens,
            "background": self.background,
            "visuals": [visual.to_dict() for visual in self.visuals],
        }


@dataclass
class ReportDocument:
    pages: Dict[str, ReportPage] = field(default_factory=dict)

    def get_page(self, name: str) -> Optional[ReportPage]:
        return self.pages.get(name)

    def add_page(self, page: ReportPage) -> None:
        if page.name in self.pages:
            raise ValueError(f"Page '{page.name}' already exists.")
        self.pages[page.name] = page

    def to_dict(self) -> Dict[str, Any]:
        return {"pages": [page.to_dict() for page in self.pages.values()]}

    def to_pbir(self) -> Dict[str, Any]:
        return {
            "pages": [page.to_page_json() for page in self.pages.values()],
            "settings": {"displayOption": "fitToPage"},
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ReportDocument":
        document = cls()
        for page_payload in payload.get("pages", []):
            page = ReportPage.from_dict(page_payload)
            document.pages[page.name] = page
        return document


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------
VISUAL_CATEGORY = {
    "card": "kpi",
    "slicer": "kpi",
    "barChart": "chart",
    "columnChart": "chart",
    "lineChart": "chart",
    "scatterChart": "chart",
    "tableEx": "detail",
    "pivotTable": "detail",
}

CATEGORY_CONFIG = {
    "kpi": {"base_y": 24, "row_height": 140},
    "chart": {"base_y": 220, "row_height": 260},
    "detail": {"base_y": 520, "row_height": 220},
}

DEFAULT_SIZES = {
    "card": (220, 140),
    "slicer": (220, 200),
    "barChart": (420, 260),
    "columnChart": (420, 260),
    "lineChart": (420, 260),
    "scatterChart": (420, 260),
    "tableEx": (560, 220),
    "pivotTable": (560, 220),
}

PADDING = 24
H_GAP = 24
V_GAP = 32


class RowState:
    """Track next placement slot for a logical row."""

    def __init__(self, base_y: float, row_height: float, page_width: float) -> None:
        self.base_y = base_y
        self.row_height = row_height
        self.page_width = page_width
        self.row_cycle = 0
        self.x_cursor = PADDING

    def reserve(self, width: float, height: float) -> Tuple[float, float]:
        self.row_height = max(self.row_height, height)
        x = self.x_cursor
        y = self.base_y + self.row_cycle * (self.row_height + V_GAP)
        if x + width > self.page_width - PADDING:
            self.row_cycle += 1
            x = PADDING
            y = self.base_y + self.row_cycle * (self.row_height + V_GAP)
        self.x_cursor = x + width + H_GAP
        return x, y


class LayoutManager:
    """Simple auto-layout grid aware of KPI/chart/detail bands."""

    def __init__(self, page: ReportPage) -> None:
        self.page = page
        self.page_width = page.size.width
        self.states: Dict[str, RowState] = {}
        self._init_states()

    def _init_states(self) -> None:
        for category, config in CATEGORY_CONFIG.items():
            self.states[category] = RowState(
                base_y=config["base_y"], row_height=config["row_height"], page_width=self.page_width
            )
        # Re-run the placement algorithm to update cursors
        for visual in sorted(self.page.visuals, key=lambda v: (v.position.y, v.position.x)):
            category = category_for_visual(visual.visual_type)
            state = self.states[category]
            state.reserve(visual.position.width, visual.position.height)

    def allocate(self, visual_type: str, width: float, height: float) -> VisualPosition:
        category = category_for_visual(visual_type)
        state = self.states[category]
        x, y = state.reserve(width, height)
        return VisualPosition(x=x, y=y, width=width, height=height, z=self._next_z())

    def _next_z(self) -> int:
        if not self.page.visuals:
            return 1
        return max(visual.position.z for visual in self.page.visuals) + 1

    def next_z(self) -> int:
        """Expose the upcoming z-index without mutating layout state."""

        return self._next_z()


def category_for_visual(visual_type: str) -> str:
    return VISUAL_CATEGORY.get(visual_type, "chart")


def default_size_for_visual(visual_type: str) -> Tuple[float, float]:
    return DEFAULT_SIZES.get(visual_type, (360, 240))


def _classify_binding_entry(expression: str, model: Optional[TMDLModel]) -> BindingEntry:
    normalized = expression.strip()
    if not normalized:
        return BindingEntry(value="", kind="column", raw=expression)

    def _measure_entry(name: str) -> BindingEntry:
        return BindingEntry(value=name, kind="measure", raw=expression)

    def _column_entry(query_ref: str) -> BindingEntry:
        return BindingEntry(value=query_ref, kind="column", raw=expression)

    if normalized.startswith("[") and normalized.endswith("]"):
        return _measure_entry(normalized[1:-1].strip())

    if "[" in normalized and normalized.endswith("]"):
        table_part, column_part = normalized.split("[", 1)
        table_name = table_part.strip()
        field_name = column_part[:-1].strip()
        if model:
            table = model.get_table(table_name)
            if table:
                if field_name in table.measures:
                    return _measure_entry(field_name)
                if field_name in table.columns:
                    return _column_entry(f"{table_name}[{field_name}]")
            if _measure_exists(model, field_name):
                return _measure_entry(field_name)
        return _column_entry(f"{table_name}[{field_name}]")

    if model and _measure_exists(model, normalized):
        return _measure_entry(normalized)

    return _measure_entry(normalized)


def _legacy_binding_entry(expression: str) -> BindingEntry:
    normalized = expression.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        return BindingEntry(value=normalized[1:-1].strip(), kind="measure", raw=expression)
    if "[" in normalized and normalized.endswith("]"):
        table_part, column_part = normalized.split("[", 1)
        table_name = table_part.strip()
        field_name = column_part[:-1].strip()
        return BindingEntry(value=f"{table_name}[{field_name}]", kind="column", raw=expression)
    return BindingEntry(value=normalized, kind="measure", raw=expression)


def _measure_exists(model: TMDLModel, measure_name: str) -> bool:
    for table in model.tables.values():
        if measure_name in table.measures:
            return True
    return False


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
REPORT_PATH_KEY = "report_path"


def add_report_page_handler(
    page: str,
    display_name: Optional[str] = None,
    size: Optional[Dict[str, Any]] = None,
    theme_tokens: Optional[Dict[str, Any]] = None,
    background: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    report_path, document = _load_report(context)
    if document.get_page(page):
        raise ValueError(f"Page '{page}' already exists.")
    validator = _build_pbir_validator(context)
    page_obj = ReportPage(
        name=page,
        display_name=display_name or page,
        size=PageSize.from_dict(size or {}),
        theme_tokens=theme_tokens or {},
        background=background,
    )
    validator.validate_page(page_obj.to_page_json())
    document.add_page(page_obj)
    _persist_report(report_path, document)
    return {"status": "success", "page": page}


def set_page_layout_handler(
    page: str,
    size: Optional[Dict[str, Any]] = None,
    theme_tokens: Optional[Dict[str, Any]] = None,
    background: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    report_path, document = _load_report(context)
    page_obj = document.get_page(page)
    if not page_obj:
        raise ValueError(f"Page '{page}' not found.")
    if size:
        page_obj.size = PageSize.from_dict(size)
    if theme_tokens is not None:
        page_obj.theme_tokens = theme_tokens
    if background is not None:
        page_obj.background = background
    validator = _build_pbir_validator(context)
    validator.validate_page(page_obj.to_page_json())
    _persist_report(report_path, document)
    return {"status": "success", "page": page}


def add_visual_handler(
    page: str,
    visual_type: str,
    bindings: Dict[str, List[str]],
    position: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    filters: Optional[List[str]] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    report_path, document = _load_report(context)
    page_obj = document.get_page(page)
    if not page_obj:
        raise ValueError(f"Page '{page}' not found.")
    model = _load_model_from_context(context)
    validator = _build_pbir_validator(context, model=model)

    default_width, default_height = default_size_for_visual(visual_type)
    width = (position or {}).get("width", default_width)
    height = (position or {}).get("height", default_height)

    layout = LayoutManager(page_obj)
    requested_position = position or {}
    manual_xy = any(coord in requested_position for coord in ("x", "y"))
    if manual_xy:
        computed_position = VisualPosition(
            x=requested_position.get("x", PADDING),
            y=requested_position.get("y", PADDING),
            width=width,
            height=height,
            z=requested_position.get("z", layout.next_z()),
        )
    else:
        computed_position = layout.allocate(visual_type, width=width, height=height)
        if "z" in requested_position:
            computed_position.z = requested_position["z"]

    visual = VisualContainer(
        visual_id=_generate_visual_id(page_obj),
        visual_type=visual_type,
        title=title,
        bindings=VisualBinding.from_mapping(bindings, model=model),
        filters=filters or [],
        position=computed_position,
    )
    visual_json = visual.to_visual_json()
    validator.validate_visual(
        visual_json,
        page_bounds=(page_obj.size.width, page_obj.size.height),
    )
    page_json = page_obj.to_page_json()
    page_json.setdefault("visualContainers", []).append(visual_json)
    validator.validate_page(page_json)
    page_obj.visuals.append(visual)
    _persist_report(report_path, document)
    return {"status": "success", "visual_id": visual.visual_id, "page": page}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _load_report(context: Optional[Dict[str, Any]]) -> Tuple[Path, ReportDocument]:
    if not context or REPORT_PATH_KEY not in context:
        raise ValueError("Tool handlers require 'report_path' inside context.")
    report_path = Path(context[REPORT_PATH_KEY]).expanduser()
    if not report_path.exists():
        return report_path, ReportDocument()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    payload: Any
    if isinstance(data, dict):
        if "internal" in data:
            payload = data["internal"]
        elif "pbir" in data:
            payload = data["pbir"]
        else:
            payload = data
    else:
        payload = data
    if not isinstance(payload, dict):
        payload = {}
    return report_path, ReportDocument.from_dict(payload)


def _persist_report(report_path: Path, document: ReportDocument) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pbir": document.to_pbir(),
        "internal": document.to_dict(),
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _generate_visual_id(page: ReportPage) -> str:
    existing_ids = {visual.visual_id for visual in page.visuals}
    while True:
        candidate = f"visual_{uuid.uuid4().hex[:8]}"
        if candidate not in existing_ids:
            return candidate


def _load_model_from_context(context: Optional[Dict[str, Any]]) -> Optional[TMDLModel]:
    if not context or MODEL_PATH_KEY not in context:
        return None
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    if not model_path.exists():
        return None
    return load_model(model_path)


def _build_pbir_validator(
    context: Optional[Dict[str, Any]],
    *,
    model: Optional[TMDLModel] = None,
) -> PBIRValidator:
    effective_model = model or _load_model_from_context(context)
    return PBIRValidator(model=effective_model)
