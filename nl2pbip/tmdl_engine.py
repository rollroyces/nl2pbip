"""TMDL (Tabular Model Definition Language) parser, writer, and tool handlers.

This module provides:
- Dataclasses for tables, columns, measures, relationships, roles, and
  the top-level ``TMDLModel``.
- A small recursive-descent-ish TMDL parser sufficient for the dialect
  used by Microsoft Power BI Project files (PBIP).
- Tool handlers invoked by the orchestrator (create_table, add_measure,
  add_pattern_measure, define_relationship, add_calculation_group,
  add_rls_role, add_ols_role).
- The path-based loader ``load_model`` used by ``pbir_engine``.

The TMDL dialect accepted/serialized here covers the subset of the spec
needed for the reference ``SalesInsights`` artifact shipped under
``artifacts/``. It is intentionally permissive — we never reject a
property we don't understand, but we DO raise ``TMDLValidationError``
(imported from ``tmdl_linter``) on structural issues caught by the
linter, so the orchestrator's retry loop has something to feed back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nl2pbip.dax_catalog import DAXCatalog
from nl2pbip.tmdl_linter import TMDLValidationError

MODEL_PATH_KEY = "model_path"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

VALID_DATA_TYPES = {
    "string",
    "int64",
    "int32",
    "double",
    "decimal",
    "boolean",
    "dateTime",
    "date",
    "time",
    "binary",
    "wholeNumber",
    "decimalNumber",
    "currency",
    "percentage",
    "text",
    "trueFalse",
    "dateTime64",
    "dateTimeLocal",
}


@dataclass
class TMDLColumn:
    name: str
    data_type: str = "string"
    source_column: Optional[str] = None
    format_string: Optional[str] = None
    description: Optional[str] = None
    annotations: Dict[str, str] = field(default_factory=dict)

    def to_tmdl(self, indent: int = 2) -> str:
        prefix = " " * indent
        lines: List[str] = [f'{prefix}column "{self.name}" {{']
        lines.append(f"{prefix}  dataType = {self.data_type}")
        if self.format_string:
            lines.append(f'{prefix}  formatString = "{self.format_string}"')
        if self.source_column:
            lines.append(f'{prefix}  sourceColumn = "{self.source_column}"')
        if self.description:
            lines.append(f'{prefix}  description = "{self._escape(self.description)}"')
        if self.annotations:
            lines.append(f"{prefix}  annotations = {{")
            for k, v in self.annotations.items():
                lines.append(f'{prefix}    {k} = "{self._escape(v)}"')
            lines.append(f"{prefix}  }}")
        lines.append(f"{prefix}}}")
        return "\n".join(lines)

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class TMDLMeasure:
    name: str
    expression: str
    format_string: Optional[str] = None
    description: Optional[str] = None
    annotations: Dict[str, str] = field(default_factory=dict)

    def to_tmdl(self, indent: int = 2) -> str:
        prefix = " " * indent
        expr_block = self._format_expression(self.expression, indent + 2)
        lines: List[str] = [f'{prefix}measure "{self.name}" {{']
        lines.append(f"{prefix}  expression = {expr_block}")
        if self.format_string:
            lines.append(f'{prefix}  formatString = "{self.format_string}"')
        if self.description:
            lines.append(f'{prefix}  description = "{self._escape(self.description)}"')
        if self.annotations:
            lines.append(f"{prefix}  annotations = {{")
            for k, v in self.annotations.items():
                lines.append(f'{prefix}    {k} = "{self._escape(v)}"')
            lines.append(f"{prefix}  }}")
        lines.append(f"{prefix}}}")
        return "\n".join(lines)

    @staticmethod
    def _format_expression(expression: str, indent: int) -> str:
        """Render expression as a TMDL triple-quoted block, preserving newlines."""
        body = expression.strip()
        prefix = " " * indent
        return f"'''\n{prefix}{body}\n{prefix}'''"

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class TMDLTable:
    name: str
    columns: Dict[str, TMDLColumn] = field(default_factory=dict)
    measures: Dict[str, TMDLMeasure] = field(default_factory=dict)
    partitions: List[Dict[str, Any]] = field(default_factory=list)
    description: Optional[str] = None
    annotations: Dict[str, str] = field(default_factory=dict)

    def add_column(self, column: TMDLColumn) -> None:
        if column.name in self.columns:
            raise TMDLValidationError(
                f"Table '{self.name}' already has a column named '{column.name}'."
            )
        self.columns[column.name] = column

    def add_measure(self, measure: TMDLMeasure) -> None:
        if measure.name in self.measures:
            raise TMDLValidationError(
                f"Table '{self.name}' already has a measure named '{measure.name}'."
            )
        self.measures[measure.name] = measure

    def to_tmdl(self) -> str:
        lines: List[str] = [f'table "{self.name}" {{']
        if self.description:
            lines.append(f'  description = "{self.description}"')
        if self.partitions:
            for partition in self.partitions:
                lines.append("  partition " + _render_partition(partition))
        if self.columns:
            lines.append("  columns = [")
            lines.append(
                ",\n".join(col.to_tmdl(indent=4) for col in self.columns.values())
            )
            lines.append("  ]")
        if self.measures:
            lines.append("  measures = [")
            lines.append(
                ",\n".join(m.to_tmdl(indent=4) for m in self.measures.values())
            )
            lines.append("  ]")
        if self.annotations:
            lines.append("  annotations = {")
            for k, v in self.annotations.items():
                lines.append(f'    {k} = "{v}"')
            lines.append("  }")
        lines.append("}")
        return "\n".join(lines)


def _render_partition(partition: Dict[str, Any]) -> str:
    """Render a partition block.

    Supports the common M-partition shape: ``source = {...}``. Returns a
    single-line block suitable for inline placement.
    """
    name = (
        partition.get("name")
        or partition.get("source", {}).get("entityName")
        or "Partition"
    )
    mode = partition.get("mode") or "import"
    source = partition.get("source") or {}
    if source:
        source_text = _render_source(source)
        return f'"{name}" = mode: {mode}, source: {source_text}'
    return f'"{name}" = mode: {mode}'


def _render_source(source: Dict[str, Any]) -> str:
    """Render a TMDL source expression block (compact)."""
    parts: List[str] = []
    src_type = source.get("type") or source.get("expressionSource") or "m"
    parts.append(f"type: {src_type}")
    for key, value in source.items():
        if key in ("type", "expressionSource"):
            continue
        if isinstance(value, str):
            parts.append(f'{key} = "{value}"')
        elif isinstance(value, bool):
            parts.append(f"{key} = {str(value).lower()}")
        else:
            parts.append(f"{key} = {value}")
    return "{ " + ", ".join(parts) + " }"


@dataclass
class TMDLRelationship:
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str = "manyToOne"
    cross_filter_direction: str = "single"
    is_active: bool = True
    from_cardinality: Optional[str] = None
    to_cardinality: Optional[str] = None

    def to_tmdl(self) -> str:
        lines: List[str] = [f'relationship "{self.name}" {{']
        lines.append(f'  fromTable = "{self.from_table}"')
        lines.append(f'  fromColumn = "{self.from_column}"')
        lines.append(f'  toTable = "{self.to_table}"')
        lines.append(f'  toColumn = "{self.to_column}"')
        lines.append(f"  cardinality = {self.cardinality}")
        if self.from_cardinality and self.to_cardinality:
            lines.append(f'  fromCardinality = "{self.from_cardinality}"')
            lines.append(f'  toCardinality = "{self.to_cardinality}"')
        lines.append(f"  crossFilterDirection = {self.cross_filter_direction}")
        lines.append(f"  isActive = {str(self.is_active).lower()}")
        lines.append("}")
        return "\n".join(lines)


@dataclass
class TMDLRolePermission:
    table_name: str
    filter_expression: str


@dataclass
class TMDLRole:
    name: str
    model_permission: Optional[str] = None
    table_permissions: List[TMDLRolePermission] = field(default_factory=list)
    hidden_tables: List[str] = field(default_factory=list)
    hidden_columns: List[Tuple[str, str]] = field(default_factory=list)

    def to_tmdl(self) -> str:
        lines: List[str] = [f'role "{self.name}" {{']
        if self.model_permission:
            lines.append(f'  modelPermission = "{self.model_permission}"')
        if self.table_permissions:
            lines.append("  tablePermissions = [")
            for perm in self.table_permissions:
                expr = perm.filter_expression.strip()
                lines.append(
                    "    "
                    + f"\"{perm.table_name}\" = filterExpression: ''\n      {expr}\n      ''"
                )
            lines.append("  ]")
        for table in self.hidden_tables:
            lines.append(f'  "{table}" = metadataPermission = none')
        for table_name, column_name in self.hidden_columns:
            lines.append(
                f'  "{table_name}" = column "{column_name}" metadataPermission = none'
            )
        lines.append("}")
        return "\n".join(lines)


@dataclass
class TMDLModel:
    name: str = "Model"
    tables: Dict[str, TMDLTable] = field(default_factory=dict)
    relationships: List[TMDLRelationship] = field(default_factory=list)
    roles: Dict[str, TMDLRole] = field(default_factory=dict)
    default_annotations: Dict[str, str] = field(default_factory=dict)

    def get_table(self, name: str) -> Optional[TMDLTable]:
        return self.tables.get(name)

    def get_measure(self, name: str) -> Optional[Tuple[TMDLTable, TMDLMeasure]]:
        for table in self.tables.values():
            if name in table.measures:
                return table, table.measures[name]
        return None

    def add_table(self, table: TMDLTable) -> None:
        if table.name in self.tables:
            raise TMDLValidationError(
                f"Model already contains a table named '{table.name}'."
            )
        self.tables[table.name] = table

    def add_relationship(self, relationship: TMDLRelationship) -> None:
        self.relationships.append(relationship)

    def add_role(self, role: TMDLRole) -> None:
        self.roles[role.name] = role


# ---------------------------------------------------------------------------
# Workspace helpers (roles directory alongside model.tmdl)
# ---------------------------------------------------------------------------


def roles_workspace_dir(model_path: Path) -> Path:
    """Return the roles workspace directory associated with ``model_path``.

    Roles live next to ``model.tmdl`` under ``.roles/``. We key on the
    parent directory + ``.roles`` so two models in different folders don't
    collide.
    """
    return model_path.parent / ".roles"


# ---------------------------------------------------------------------------
# Parser (round-trippable enough for orchestrator output)
# ---------------------------------------------------------------------------

_SECTION_PATTERN = re.compile(
    r'(?P<header>table|column|measure|relationship|role)\s+("(?P<qname>[^"]+)"|(?P<name>\S+))\s*\{',
    re.IGNORECASE,
)


def parse_tmdl_text(text: str) -> TMDLModel:
    """Parse TMDL text into a ``TMDLModel``.

    Supports the dialect emitted by this module's own writers. Unknown
    blocks are skipped silently to keep forward compatibility with newer
    Power BI TMDL extensions.
    """
    model = TMDLModel()
    text = text or ""
    cursor = 0

    while cursor < len(text):
        match = _SECTION_PATTERN.search(text, cursor)
        if not match:
            break
        section_type = match.group("header").lower()
        name = match.group("qname") or match.group("name") or ""
        body_start = match.end() - 1  # the opening '{' char
        body, body_end = _extract_balanced(text, body_start)
        if body is None:
            raise TMDLValidationError(
                f"Unbalanced braces while parsing section '{section_type} {name}'."
            )
        if section_type == "table":
            model.add_table(_parse_table(name, body))
        elif section_type == "relationship":
            model.add_relationship(_parse_relationship(name, body))
        elif section_type == "role":
            model.roles[name] = _parse_role(name, body)
        # column / measure sections outside a table are ignored here —
        # tables re-emit their own column/measure blocks.
        cursor = body_end + 1

    return model


def _extract_balanced(text: str, open_index: int) -> Tuple[Optional[str], int]:
    """Return (inner_text, close_index) for the braces starting at ``open_index``."""
    return _extract_balanced_with(text, open_index, "{", "}")


def _extract_balanced_with(
    text: str, open_index: int, open_char: str, close_char: str
) -> Tuple[Optional[str], int]:
    """Variant of ``_extract_balanced`` that accepts an arbitrary bracket pair."""
    if open_index >= len(text) or text[open_index] != open_char:
        return None, -1
    depth = 0
    in_string = False
    string_quote = ""
    triple = False
    i = open_index
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if not in_string and char == "'" and nxt == "'" and text[i + 2 : i + 3] == "'":
            in_string = True
            triple = True
            string_quote = "'''"
            i += 3
            continue
        if (
            in_string
            and triple
            and char == "'"
            and nxt == "'"
            and text[i + 2 : i + 3] == "'"
        ):
            in_string = False
            triple = False
            string_quote = ""
            i += 3
            continue
        if not triple:
            if not in_string and char == '"':
                in_string = True
                string_quote = '"'
                i += 1
                continue
            if in_string and char == string_quote and (i == 0 or text[i - 1] != "\\"):
                in_string = False
                string_quote = ""
                i += 1
                continue
            if not in_string:
                if char == open_char:
                    depth += 1
                elif char == close_char:
                    depth -= 1
                    if depth == 0:
                        return text[open_index + 1 : i], i
        i += 1
    return None, -1


def _parse_table(name: str, body: str) -> TMDLTable:
    table = TMDLTable(name=name)
    columns_payload = _find_block(body, "columns")
    if columns_payload:
        table.columns = _parse_columns(columns_payload)
    measures_payload = _find_block(body, "measures")
    if measures_payload:
        table.measures = _parse_measures(measures_payload)
    return table


def _parse_columns(block: str) -> Dict[str, TMDLColumn]:
    columns: Dict[str, TMDLColumn] = {}
    cursor = 0
    while cursor < len(block):
        match = _SECTION_PATTERN.search(block, cursor)
        if not match or match.group("header").lower() != "column":
            break
        col_name = match.group("qname") or match.group("name") or ""
        body_start = match.end() - 1
        body, body_end = _extract_balanced(block, body_start)
        if body is None:
            break
        cols = _parse_column(col_name, body)
        columns[cols.name] = cols
        cursor = body_end + 1
    return columns


def _parse_column(name: str, body: str) -> TMDLColumn:
    data_type = _extract_scalar(body, "dataType") or "string"
    return TMDLColumn(
        name=name,
        data_type=data_type,
        format_string=_extract_scalar(body, "formatString"),
        source_column=_extract_scalar(body, "sourceColumn"),
        description=_extract_scalar(body, "description"),
    )


def _parse_measures(block: str) -> Dict[str, TMDLMeasure]:
    measures: Dict[str, TMDLMeasure] = {}
    cursor = 0
    while cursor < len(block):
        match = _SECTION_PATTERN.search(block, cursor)
        if not match or match.group("header").lower() != "measure":
            break
        measure_name = match.group("qname") or match.group("name") or ""
        body_start = match.end() - 1
        body, body_end = _extract_balanced(block, body_start)
        if body is None:
            break
        m = _parse_measure(measure_name, body)
        measures[m.name] = m
        cursor = body_end + 1
    return measures


def _parse_measure(name: str, body: str) -> TMDLMeasure:
    expression = _extract_expression(body) or ""
    return TMDLMeasure(
        name=name,
        expression=expression,
        format_string=_extract_scalar(body, "formatString"),
        description=_extract_scalar(body, "description"),
    )


def _parse_relationship(name: str, body: str) -> TMDLRelationship:
    return TMDLRelationship(
        name=name,
        from_table=_extract_scalar(body, "fromTable") or "",
        from_column=_extract_scalar(body, "fromColumn") or "",
        to_table=_extract_scalar(body, "toTable") or "",
        to_column=_extract_scalar(body, "toColumn") or "",
        cardinality=_extract_scalar(body, "cardinality") or "manyToOne",
        cross_filter_direction=_extract_scalar(body, "crossFilterDirection")
        or "single",
        is_active=(_extract_scalar(body, "isActive") or "true").lower() == "true",
    )


def _parse_role(name: str, body: str) -> TMDLRole:
    role = TMDLRole(
        name=name, model_permission=_extract_scalar(body, "modelPermission")
    )
    block = _find_block(body, "tablePermissions")
    if block:
        for perm_name, perm_body in _iter_named_blocks(block):
            expr = _extract_expression(perm_body) or ""
            role.table_permissions.append(
                TMDLRolePermission(table_name=perm_name, filter_expression=expr)
            )
    return role


def _find_block(text: str, key: str) -> Optional[str]:
    """Return the inner content of the first ``key = { ... }`` or ``key = [ ... ]`` block."""
    pattern = re.compile(rf"\b{re.escape(key)}\s*=\s*([\[\{{])", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    open_char = match.group(1)
    close_char = "]" if open_char == "[" else "}"
    idx = match.end() - 1
    inner, _ = _extract_balanced_with(text, idx, open_char, close_char)
    return inner


def _iter_named_blocks(text: str) -> Iterable[Tuple[str, str]]:
    cursor = 0
    while cursor < len(text):
        match = _SECTION_PATTERN.search(text, cursor)
        if not match:
            break
        name = match.group("qname") or match.group("name") or ""
        body_start = match.end() - 1
        body, body_end = _extract_balanced(text, body_start)
        if body is None:
            break
        yield name, body
        cursor = body_end + 1


def _extract_scalar(body: str, key: str) -> Optional[str]:
    pattern = re.compile(
        rf'\b{re.escape(key)}\s*=\s*("[^"]*"|\'[^\']*\')', re.IGNORECASE
    )
    match = pattern.search(body)
    if not match:
        pattern2 = re.compile(rf"\b{re.escape(key)}\s*=\s*([^\s,]+)", re.IGNORECASE)
        match = pattern2.search(body)
        if not match:
            return None
        return match.group(1).strip("\"'")
    return match.group(1).strip("\"'")


def _extract_expression(body: str) -> Optional[str]:
    """Extract a triple-quoted expression body, if present."""
    match = re.search(r"expression\s*=\s*'''", body)
    if not match:
        single = re.search(r"expression\s*=\s*([^\n]+)", body)
        if single:
            return single.group(1).strip()
        return None
    start = match.end()
    end = body.find("'''", start)
    if end == -1:
        return body[start:].strip()
    return body[start:end].strip()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_model(model_path: Path) -> TMDLModel:
    """Load a TMDL model from a single ``model.tmdl`` file.

    Roles are loaded from ``<model_dir>/.roles/*.tmdl`` if present.
    """
    if not model_path.exists():
        raise FileNotFoundError(f"TMDL model file not found at {model_path}.")
    model = parse_tmdl_text(model_path.read_text(encoding="utf-8"))
    roles_dir = roles_workspace_dir(model_path)
    if roles_dir.exists():
        for role_file in sorted(roles_dir.glob("*.tmdl")):
            role_model = parse_tmdl_text(role_file.read_text(encoding="utf-8"))
            for name, role in role_model.roles.items():
                model.roles[name] = role
    return model


# ---------------------------------------------------------------------------
# Handler helpers
# ---------------------------------------------------------------------------


def _validate_columns(table_name: str, columns: Iterable[Any]) -> None:
    for column in columns:
        if not isinstance(column, dict) or "name" not in column:
            raise TMDLValidationError(
                f"Table '{table_name}' columns must be objects with a 'name' field."
            )
        data_type = column.get("data_type") or column.get("dataType") or "string"
        if data_type not in VALID_DATA_TYPES:
            raise TMDLValidationError(
                f"Column '{column.get('name')}' on table '{table_name}' uses unsupported "
                f"dataType '{data_type}'. Valid types: {sorted(VALID_DATA_TYPES)}."
            )


def _persist_model(context: Dict[str, Any]) -> Path:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("Tool handlers require 'model_path' inside context.")
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model = load_model(model_path)
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return model_path


def _render_model_body(model: TMDLModel) -> str:
    parts: List[str] = []
    if model.relationships:
        parts.append("\n\n".join(rel.to_tmdl() for rel in model.relationships))
    for table in model.tables.values():
        parts.append(table.to_tmdl())
    return ("\n\n".join(parts).strip() + "\n") if parts else ""


def _save_role(context: Dict[str, Any], role: TMDLRole) -> Path:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("Tool handlers require 'model_path' inside context.")
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    roles_dir = roles_workspace_dir(model_path)
    roles_dir.mkdir(parents=True, exist_ok=True)
    target = roles_dir / f"{role.name}.tmdl"
    target.write_text(role.to_tmdl() + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def create_table_handler(
    table_name: str,
    columns: List[Dict[str, Any]],
    measures: Optional[List[Dict[str, Any]]] = None,
    source: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("create_table handler requires 'model_path' inside context.")
    if not isinstance(columns, list) or not columns:
        raise TMDLValidationError("create_table requires a non-empty 'columns' array.")
    _validate_columns(table_name, columns)

    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    if model_path.exists():
        model = load_model(model_path)
    else:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model = TMDLModel()
    if model.get_table(table_name):
        raise TMDLValidationError(f"Table '{table_name}' already exists in the model.")

    table = TMDLTable(name=table_name)
    if source:
        table.partitions.append(source)
    for col in columns:
        table.add_column(
            TMDLColumn(
                name=col["name"],
                data_type=col.get("data_type") or col.get("dataType") or "string",
                format_string=col.get("format_string") or col.get("formatString"),
                source_column=col.get("source_column") or col.get("sourceColumn"),
                description=col.get("description"),
            )
        )
    if measures:
        for measure in measures:
            if (
                not isinstance(measure, dict)
                or "name" not in measure
                or "expression" not in measure
            ):
                raise TMDLValidationError(
                    "create_table 'measures' entries must include 'name' and 'expression'."
                )
            table.add_measure(
                TMDLMeasure(
                    name=measure["name"],
                    expression=measure["expression"],
                    format_string=measure.get("format_string")
                    or measure.get("formatString"),
                    description=measure.get("description"),
                )
            )

    model.add_table(table)
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return {
        "status": "success",
        "table": table_name,
        "columns": list(table.columns.keys()),
        "measures": list(table.measures.keys()),
        "model_path": str(model_path),
    }


def add_measure_handler(
    table_name: str,
    measure_name: str,
    expression: str,
    format_string: Optional[str] = None,
    description: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("add_measure handler requires 'model_path' inside context.")
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    model = load_model(model_path) if model_path.exists() else TMDLModel()
    table = model.get_table(table_name)
    if not table:
        raise TMDLValidationError(
            f"Cannot add measure '{measure_name}': table '{table_name}' does not exist."
        )
    if measure_name in table.measures:
        existing = table.measures[measure_name]
        if existing.expression.strip() == expression.strip():
            return {
                "status": "noop",
                "table": table_name,
                "measure": measure_name,
                "model_path": str(model_path),
            }
        raise TMDLValidationError(
            f"Measure '{measure_name}' already exists on table '{table_name}'."
        )
    measure = TMDLMeasure(
        name=measure_name,
        expression=expression,
        format_string=format_string,
        description=description,
    )
    table.add_measure(measure)
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return {
        "status": "success",
        "table": table_name,
        "measure": measure_name,
        "model_path": str(model_path),
    }


def add_pattern_measure_handler(
    table_name: str,
    measure_name: str,
    pattern_key: str,
    base_measure: str,
    format_override: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not context:
        raise ValueError("add_pattern_measure handler requires 'context'.")
    catalog_path = context.get("dax_catalog_path")
    if not catalog_path:
        raise ValueError(
            "add_pattern_measure handler requires 'dax_catalog_path' inside context."
        )
    catalog = DAXCatalog.from_file(str(catalog_path))
    params: Dict[str, Any] = {"base_measure": base_measure}
    if parameters:
        params.update(parameters)
    rendered = catalog.render_pattern(
        pattern_key, **{k: str(v) for k, v in params.items()}
    )
    return add_measure_handler(
        table_name=table_name,
        measure_name=measure_name,
        expression=rendered["expression"],
        format_string=format_override or rendered.get("format_string"),
        context=context,
    )


def define_relationship_handler(
    from_table: str,
    from_column: str,
    to_table: str,
    to_column: str,
    cardinality: str = "manyToOne",
    cross_filter_direction: str = "single",
    active: bool = True,
    name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError(
            "define_relationship handler requires 'model_path' inside context."
        )
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    model = load_model(model_path) if model_path.exists() else TMDLModel()
    if not model.get_table(from_table):
        raise TMDLValidationError(
            f"Relationship references unknown table '{from_table}'."
        )
    if not model.get_table(to_table):
        raise TMDLValidationError(
            f"Relationship references unknown table '{to_table}'."
        )
    rel_name = name or f"{from_table}_{from_column}_{to_table}_{to_column}"
    if any(r.name == rel_name for r in model.relationships):
        return {
            "status": "noop",
            "relationship": rel_name,
            "model_path": str(model_path),
        }
    relationship = TMDLRelationship(
        name=rel_name,
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        to_column=to_column,
        cardinality=cardinality,
        cross_filter_direction=cross_filter_direction,
        is_active=active,
    )
    model.add_relationship(relationship)
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return {
        "status": "success",
        "relationship": rel_name,
        "model_path": str(model_path),
    }


def add_calculation_group_handler(
    group_key: str,
    table_name: Optional[str] = None,
    precedence: int = 0,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not context:
        raise ValueError("add_calculation_group handler requires 'context'.")
    catalog_path = context.get("dax_catalog_path")
    if not catalog_path:
        raise ValueError(
            "add_calculation_group handler requires 'dax_catalog_path' inside context."
        )
    catalog = DAXCatalog.from_file(str(catalog_path))
    group = catalog.get_calculation_group(group_key)
    chosen_name = table_name or group.table_name
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError(
            "add_calculation_group handler requires 'model_path' inside context."
        )
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    model = load_model(model_path) if model_path.exists() else TMDLModel()
    if model.get_table(chosen_name):
        return {
            "status": "noop",
            "table": chosen_name,
            "model_path": str(model_path),
        }
    table = TMDLTable(name=chosen_name)
    table.annotations["calculationGroupPrecedence"] = str(
        group.precedence or precedence
    )
    column = TMDLColumn(name="Name", data_type="string")
    table.add_column(column)
    column2 = TMDLColumn(name="Ordinal", data_type="wholeNumber")
    table.add_column(column2)
    for ordinal, item in enumerate(group.items, start=1):
        item_name = item.get("name") or f"Item_{ordinal}"
        expression = item.get("expression") or "SELECTEDMEASURE()"
        format_string = item.get("format_string") or item.get("formatString")
        table.add_measure(
            TMDLMeasure(
                name=item_name,
                expression=expression,
                format_string=format_string,
            )
        )
    model.add_table(table)
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return {
        "status": "success",
        "table": chosen_name,
        "items": [item.get("name") for item in group.items if item.get("name")],
        "model_path": str(model_path),
    }


def add_rls_role_handler(
    role_name: str,
    table_permissions: List[Dict[str, Any]],
    model_permission: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("add_rls_role handler requires 'model_path' inside context.")
    if not table_permissions:
        raise TMDLValidationError(
            "add_rls_role requires at least one table_permission entry."
        )
    role = TMDLRole(name=role_name, model_permission=model_permission)
    for entry in table_permissions:
        if not isinstance(entry, dict):
            raise TMDLValidationError("table_permission entries must be objects.")
        table_name = entry.get("table_name") or entry.get("tableName")
        filter_expr = entry.get("filter_expression") or entry.get("filterExpression")
        if not table_name or not filter_expr:
            raise TMDLValidationError(
                "Each table_permission must include 'table_name' and 'filter_expression'."
            )
        role.table_permissions.append(
            TMDLRolePermission(table_name=table_name, filter_expression=filter_expr)
        )
    target = _save_role(context, role)
    return {
        "status": "success",
        "role": role_name,
        "tables": [p.table_name for p in role.table_permissions],
        "role_path": str(target),
    }


def add_ols_role_handler(
    role_name: str,
    hidden_tables: Optional[List[str]] = None,
    hidden_columns: Optional[List[Dict[str, Any]]] = None,
    model_permission: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("add_ols_role handler requires 'model_path' inside context.")
    if not hidden_tables and not hidden_columns:
        raise TMDLValidationError(
            "add_ols_role requires at least one hidden table or hidden column."
        )
    role = TMDLRole(name=role_name, model_permission=model_permission)
    if hidden_tables:
        role.hidden_tables.extend(hidden_tables)
    if hidden_columns:
        for entry in hidden_columns:
            if not isinstance(entry, dict):
                raise TMDLValidationError("hidden_columns entries must be objects.")
            table_name = entry.get("table_name") or entry.get("tableName")
            column_name = entry.get("column_name") or entry.get("columnName")
            if not table_name or not column_name:
                raise TMDLValidationError(
                    "hidden_columns entries must include 'table_name' and 'column_name'."
                )
            role.hidden_columns.append((table_name, column_name))
    target = _save_role(context, role)
    return {
        "status": "success",
        "role": role_name,
        "hidden_tables": role.hidden_tables,
        "hidden_columns": [
            {"table_name": t, "column_name": c} for t, c in role.hidden_columns
        ],
        "role_path": str(target),
    }


__all__ = [
    "MODEL_PATH_KEY",
    "TMDLModel",
    "TMDLTable",
    "TMDLColumn",
    "TMDLMeasure",
    "TMDLRelationship",
    "TMDLRole",
    "TMDLRolePermission",
    "VALID_DATA_TYPES",
    "create_table_handler",
    "add_measure_handler",
    "add_pattern_measure_handler",
    "define_relationship_handler",
    "add_calculation_group_handler",
    "add_rls_role_handler",
    "add_ols_role_handler",
    "load_model",
    "parse_tmdl_text",
    "roles_workspace_dir",
]
