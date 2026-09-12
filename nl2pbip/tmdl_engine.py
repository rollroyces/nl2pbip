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

from nl2pbip.data_types import (
    ACCEPTED_DATA_TYPES,
    DEFAULT_FORMAT_STRINGS,
    DataTypeError,
    normalize_data_type,
)
from nl2pbip.dax_catalog import DAXCatalog
from nl2pbip.tmdl_linter import TMDLValidationError

MODEL_PATH_KEY = "model_path"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Backwards-compat export: the original constant name is preserved so
# downstream code that imports ``VALID_DATA_TYPES`` keeps working. The
# canonical set is now maintained in :mod:`nl2pbip.data_types` along
# with alias resolution and default format suggestions.
VALID_DATA_TYPES = ACCEPTED_DATA_TYPES


@dataclass
class TMDLColumn:
    name: str
    data_type: str = "string"
    source_column: Optional[str] = None
    format_string: Optional[str] = None
    description: Optional[str] = None
    annotations: Dict[str, str] = field(default_factory=dict)
    # ``is_hidden`` corresponds to the ``isHidden`` TMDL marker on a
    # column. Used by field-parameter tables for the Fields and
    # Ordinal columns (the user-visible column is the only one that's
    # actually shown in slicers).
    is_hidden: bool = False
    # ``is_name_inferred`` corresponds to the ``isNameInferred`` TMDL
    # marker. Power BI sets this on field-parameter name columns so the
    # semantic engine knows the column name is derived from the table
    # expression rather than a literal source column.
    is_name_inferred: bool = False
    # ``sort_by_column`` corresponds to the ``sortByColumn`` TMDL
    # marker. Used by field-parameter tables so the slicer respects
    # the ordinal ordering.
    sort_by_column: Optional[str] = None
    # Original spelling supplied by the caller, before alias
    # resolution. Useful for error messages — "you said 'bigint',
    # we wrote 'int64'". May be ``None`` if the column was loaded
    # from disk where the canonical form is already canonical.
    original_data_type: Optional[str] = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Reject non-strings immediately. Unknown types raise so the
        # error is surfaced in the LLM retry loop rather than
        # silently producing a corrupt TMDL file.
        if not isinstance(self.data_type, str):
            raise TMDLValidationError(
                f"Column '{self.name}' data_type must be a string, "
                f"got {type(self.data_type).__name__}."
            )
        # Preserve the original spelling before alias rewriting.
        self.original_data_type = self.data_type
        try:
            self.data_type = normalize_data_type(self.data_type)
        except DataTypeError as exc:
            raise TMDLValidationError(
                f"Column '{self.name}' has unsupported dataType: {exc}"
            ) from exc
        # Auto-suggest a format string when the caller didn't supply
        # one. This makes "Amount: decimal" produce "$#,0.00" without
        # the LLM needing to remember to spell it out.
        if self.format_string is None and self.data_type in DEFAULT_FORMAT_STRINGS:
            self.format_string = DEFAULT_FORMAT_STRINGS[self.data_type]

    def to_tmdl(self, indent: int = 2) -> str:
        prefix = " " * indent
        lines: List[str] = [f'{prefix}column "{self.name}" {{']
        lines.append(f"{prefix}  dataType = {self.data_type}")
        if self.format_string:
            lines.append(f'{prefix}  formatString = "{self.format_string}"')
        if self.source_column:
            lines.append(f'{prefix}  sourceColumn = "{self.source_column}"')
        if self.sort_by_column:
            lines.append(f'{prefix}  sortByColumn = "{self.sort_by_column}"')
        if self.is_name_inferred:
            # ``isNameInferred`` is a marker keyword (no value).
            lines.append(f"{prefix}  isNameInferred")
        if self.is_hidden:
            lines.append(f"{prefix}  isHidden")
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
        expr_block = _format_expression(self.expression, indent + 2)
        lines: List[str] = [f'{prefix}measure "{self.name}" {{']
        lines.append(f"{prefix}  expression = {expr_block}")
        if self.format_string:
            lines.append(f'{prefix}  formatString = "{_escape(self.format_string)}"')
        if self.description:
            lines.append(f'{prefix}  description = "{_escape(self.description)}"')
        if self.annotations:
            lines.append(f"{prefix}  annotations = {{")
            for k, v in self.annotations.items():
                lines.append(f'{prefix}    {k} = "{_escape(v)}"')
            lines.append(f"{prefix}  }}")
        lines.append(f"{prefix}}}")
        return "\n".join(lines)


def _format_expression(expression: str, indent: int) -> str:
    """Render expression as a TMDL triple-quoted block, preserving newlines."""
    body = expression.strip()
    prefix = " " * indent
    return f"'''\n{prefix}{body}\n{prefix}'''"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class TMDLCalculationItem:
    """A single ``calculationItem`` in a TMDL calculation group table.

    Calculation items live inside a ``calculationGroup`` block on the table
    header (not as measures). They support both a static ``formatString``
    and a ``formatStringDefinition`` (dynamic format expression) keyed by
    item name.
    """

    name: str
    expression: str
    format_string: Optional[str] = None
    format_string_definition: Optional[str] = None
    description: Optional[str] = None

    def to_tmdl(self, indent: int = 2) -> str:
        """Render a single calculation item line.

        The grammar is::

            calculationItem 'YTD' =
                '''
                TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])
                '''

        with optional sibling line ``calculationItem 'YTD'
        formatStringDefinition = ...`` when a dynamic format is provided.

        ``indent`` controls the leading whitespace for the
        ``calculationItem`` keyword (callers should pass 8 to keep the
        item under ``calculationGroup``). The expression itself is
        rendered with two extra spaces of indent inside the triple-
        quoted block.
        """
        prefix = " " * indent
        expr_prefix = " " * (indent + 2)
        expr_text = self.expression.strip()
        expr_block = f"'''\n{expr_prefix}{expr_text}\n{expr_prefix}'''"
        lines: List[str] = [
            f"{prefix}calculationItem '{self.name}' = {expr_block}",
        ]
        if self.format_string_definition:
            fs_text = self.format_string_definition.strip()
            fs_block = f"'''\n{expr_prefix}{fs_text}\n{expr_prefix}'''"
            lines.append(
                f"{prefix}calculationItem '{self.name}' "
                f"formatStringDefinition = {fs_block}"
            )
        return "\n".join(lines)


@dataclass
class TMDLTable:
    name: str
    columns: Dict[str, TMDLColumn] = field(default_factory=dict)
    measures: Dict[str, TMDLMeasure] = field(default_factory=dict)
    partitions: List[Dict[str, Any]] = field(default_factory=list)
    description: Optional[str] = None
    annotations: Dict[str, str] = field(default_factory=dict)
    # Calculation-group-specific fields. When ``is_calculation_group`` is
    # true the table emits a ``calculationGroup`` block with the items below
    # instead of the normal column / partition layout. Calculation-group
    # tables are still allowed to declare their discriminator and ordinal
    # columns via the regular ``columns`` field.
    is_calculation_group: bool = False
    precedence: Optional[int] = None
    calculation_items: List[TMDLCalculationItem] = field(default_factory=list)
    # Field-parameter-specific fields. When ``is_parameter_table`` is true
    # the table emits ``isParameterTable`` (no value) under the table
    # header, in addition to the standard ``column`` blocks for the name,
    # fields, and ordinal columns. The ``parameter_partition_expression``
    # field carries the DAX table expression that produces the parameter
    # rows — typically ``{ ("Display", NAMEOF('Tbl'[Col]), Ord), ... }``.
    is_parameter_table: bool = False
    parameter_partition_expression: Optional[str] = None

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

    def add_calculation_item(self, item: TMDLCalculationItem) -> None:
        """Register a calculation item on a calculation-group table."""
        if not self.is_calculation_group:
            raise TMDLValidationError(
                f"Table '{self.name}' is not a calculation group; "
                "call mark_calculation_group() before adding items."
            )
        for existing in self.calculation_items:
            if existing.name == item.name:
                raise TMDLValidationError(
                    f"Calculation group '{self.name}' already has an item "
                    f"named '{item.name}'."
                )
        self.calculation_items.append(item)

    def mark_calculation_group(self, precedence: Optional[int] = None) -> None:
        """Convert this table into a calculation group."""
        self.is_calculation_group = True
        if precedence is not None:
            self.precedence = precedence

    def to_tmdl(self) -> str:
        if self.is_calculation_group:
            return self._to_calculation_group_tmdl()
        lines: List[str] = [f'table "{self.name}" {{']
        if self.description:
            lines.append(f'  description = "{_escape(self.description)}"')
        if self.is_parameter_table:
            # ``isParameterTable`` is a marker keyword (no value).
            lines.append("  isParameterTable")
        if self.partitions:
            for partition in self.partitions:
                lines.append("  partition " + _render_partition(partition))
        elif self.is_parameter_table and self.parameter_partition_expression:
            # Synthesise the ``partition Name = calculated expression = ...``
            # block from the parameter table expression. Power BI Desktop
            # always emits this partition in field-parameter tables.
            synth_partition = {
                "name": self.name,
                "mode": "calculated",
                "expression": self.parameter_partition_expression,
            }
            lines.append("  partition " + _render_partition(synth_partition))
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
                lines.append(f'    {k} = "{_escape(v)}"')
            lines.append("  }")
        lines.append("}")
        return "\n".join(lines)

    def _to_calculation_group_tmdl(self) -> str:
        """Render a calculation-group table in canonical TMDL.

        The grammar (per Microsoft spec, Sept 2025)::

            table 'Time Intelligence'
                calculationGroup
                    precedence: 1
                    calculationItem 'YTD' = SELECTEDMEASURE()
                    calculationItem 'YTD' formatStringDefinition = ...
                column 'Time Intelligence'
                    dataType: string
                    summarizeBy: none
                    sourceColumn: Name
                    sortByColumn: Ordinal
                column Ordinal
                    dataType: int64
                    formatString: 0
                    summarizeBy: sum
                    sourceColumn: Ordinal

        Indentation: table header at column 0, ``calculationGroup`` at
        4 spaces, ``precedence`` and items at 8 spaces.
        """
        lines: List[str] = [f"table '{self.name}' {{"]
        lines.append("    calculationGroup")
        if self.precedence is not None:
            lines.append(f"        precedence: {self.precedence}")
        if self.calculation_items:
            lines.append("")
            for item in self.calculation_items:
                lines.append(item.to_tmdl(indent=8))
        if self.columns:
            lines.append("")
            for col in self.columns.values():
                lines.append(col.to_tmdl(indent=2))
        lines.append("}")
        return "\n".join(lines)


def _render_partition(partition: Dict[str, Any]) -> str:
    """Render a partition block.

    Supports the common M-partition shape (``source = {...}``) and the
    field-parameter / calculated-table shape (``mode: calculated,
    expression: '''...'''``). Returns a single-line header for M-partitions
    or a multi-line block for calculated-table partitions so the
    triple-quoted expression stays readable.
    """
    name = (
        partition.get("name")
        or partition.get("source", {}).get("entityName")
        or "Partition"
    )
    mode = partition.get("mode") or "import"
    source = partition.get("source") or {}
    expression = partition.get("expression")
    if mode == "calculated" or expression is not None:
        expr_text = (expression or "").strip()
        # Multi-line block keeps the table expression readable in TMDL.
        if "\n" in expr_text or len(expr_text) > 60:
            return (
                f"\"{name}\" = calculated\n  expression = '''\n"
                f"{expr_text}\n"
                f"  '''"
            )
        return f"\"{name}\" = calculated expression = '''{expr_text}'''"
    if source:
        source_text = _render_source(source)
        return f'"{name}" = mode: {mode}, source: {source_text}'
    return f'"{name}" = mode: {mode}'


def _render_source(source: Dict[str, Any]) -> str:
    """Render a TMDL source expression block (compact).

    Power BI source dicts may include a multi-line M ``expression``
    string. We use a single-line ``expression = "..."`` form for
    short M queries and a multi-line ``expression = '...'`` form
    when the body contains newlines, so that the resulting TMDL
    stays readable.
    """
    parts: List[str] = []
    src_type = source.get("type") or source.get("expressionSource") or "m"
    parts.append(f"type: {src_type}")
    for key, value in source.items():
        if key in ("type", "expressionSource"):
            continue
        if key == "expression" and isinstance(value, str):
            # The M expression is stored unquoted in the source dict;
            # emit it inside an M-style double-quoted string literal
            # (embedded `"` are doubled). This is the format Power BI
            # Desktop writes when you open the .pbip in the IDE.
            # Note: we use ``m_escape`` (not ``quote_string``) because
            # the M expression text already contains its own string
            # literals — wrapping it again would double-escape.
            from nl2pbip.m_builder import m_escape as _m_escape

            parts.append(f'expression = "{_m_escape(value)}"')
        elif isinstance(value, str):
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
class TMDLColumnPermission:
    """OLS column-level permission entry.

    ``metadata_permission`` is either ``"none"`` (hide the column from
    the role) or ``"read"`` (allow read access; the default in Power BI
    Desktop is to allow read unless explicitly hidden).
    """

    table_name: str
    column_name: str
    metadata_permission: str = "none"


@dataclass
class TMDLTablePermission:
    """OLS / RLS table-level permission entry.

    Used both for RLS row filters (``filterExpression``) and OLS
    whole-table hiding (``metadataPermission``). The two are mutually
    exclusive per Microsoft spec — a single ``tablePermission`` block
    carries either filterExpression *or* metadataPermission, not both.
    Nested ``columnPermission`` blocks carry OLS column-level permissions.
    """

    table_name: str
    filter_expression: Optional[str] = None
    metadata_permission: Optional[str] = None
    columns: List[TMDLColumnPermission] = field(default_factory=list)


# Backwards-compat alias. Older call sites (and external callers that
# imported the dataclass) still get a class with the same shape.
TMDLRolePermission = TMDLTablePermission


@dataclass
class TMDLRole:
    name: str
    model_permission: Optional[str] = None
    table_permissions: List[TMDLTablePermission] = field(default_factory=list)
    hidden_tables: List[str] = field(default_factory=list)
    hidden_columns: List[Tuple[str, str]] = field(default_factory=list)
    # New OLS-native representation. When populated, ``to_tmdl`` emits
    # nested ``tablePermission`` / ``columnPermission`` blocks per the
    # Microsoft TMDL spec instead of the inline ``"Table" = metadataPermission
    # = none`` syntax. Both representations coexist: the legacy
    # ``hidden_tables`` / ``hidden_columns`` fields still parse and emit
    # valid TMDL for backward compatibility, but new code should use the
    # table-level / column-level permission lists directly.
    table_permissions_metadata: Dict[str, str] = field(default_factory=dict)
    column_permissions_metadata: Dict[Tuple[str, str], str] = field(
        default_factory=dict
    )

    def add_table_permission(
        self,
        table_name: str,
        metadata_permission: Optional[str] = None,
        filter_expression: Optional[str] = None,
    ) -> None:
        """Register an OLS table-level permission (or RLS row filter).

        Validates that ``metadata_permission`` is one of ``"none"`` or
        ``"read"``. If both ``metadata_permission`` and
        ``filter_expression`` are supplied, the handler raises — a single
        tablePermission block can carry only one of them per Microsoft
        spec.

        Calling this method twice on the same table with the *same*
        aspect (e.g. metadata_permission both times) updates the
        existing entry in place. Calling it twice with *different*
        aspects (e.g. once with filter_expression, once with
        metadata_permission) creates two separate tablePermission
        entries — one per aspect — so the result is two distinct
        ``tablePermission`` blocks in the rendered TMDL.
        """
        if metadata_permission is not None:
            self._validate_metadata_permission(metadata_permission, table_name)
        if metadata_permission is not None and filter_expression is not None:
            raise TMDLValidationError(
                f"Table '{table_name}' permission block cannot carry both "
                "metadataPermission and filterExpression."
            )
        existing = self._find_table_permission(table_name)
        if existing is not None:
            # If the existing entry already covers the same aspect,
            # update it in place. Otherwise (different aspect, or the
            # existing entry has no aspect set yet) create a sibling
            # entry so the two aspects end up as two distinct
            # tablePermission blocks.
            existing_filter = existing.filter_expression is not None
            existing_metadata = existing.metadata_permission is not None
            new_filter = filter_expression is not None
            new_metadata = metadata_permission is not None
            if (new_filter and existing_filter) or (new_metadata and existing_metadata):
                if metadata_permission is not None:
                    existing.metadata_permission = metadata_permission
                if filter_expression is not None:
                    existing.filter_expression = filter_expression
                return
            if not existing_filter and not existing_metadata:
                # Existing entry has no aspect yet — fill it in.
                if metadata_permission is not None:
                    existing.metadata_permission = metadata_permission
                if filter_expression is not None:
                    existing.filter_expression = filter_expression
                return
            # Existing covers one aspect; new call covers the other.
            # Create a sibling entry so both render as separate blocks.
        self.table_permissions.append(
            TMDLTablePermission(
                table_name=table_name,
                filter_expression=filter_expression,
                metadata_permission=metadata_permission,
            )
        )

    def add_column_permission(
        self, table_name: str, column_name: str, metadata_permission: str = "none"
    ) -> None:
        """Register an OLS column-level permission."""
        self._validate_metadata_permission(metadata_permission, column_name)
        existing_table = self._find_table_permission(table_name)
        if existing_table is None:
            existing_table = TMDLTablePermission(table_name=table_name)
            self.table_permissions.append(existing_table)
        for col in existing_table.columns:
            if col.column_name == column_name:
                col.metadata_permission = metadata_permission
                return
        existing_table.columns.append(
            TMDLColumnPermission(
                table_name=table_name,
                column_name=column_name,
                metadata_permission=metadata_permission,
            )
        )

    def _find_table_permission(
        self, table_name: str
    ) -> Optional["TMDLTablePermission"]:
        for perm in self.table_permissions:
            if perm.table_name == table_name:
                return perm
        return None

    @staticmethod
    def _validate_metadata_permission(value: str, target: str) -> None:
        if value not in ("none", "read"):
            raise TMDLValidationError(
                f"metadataPermission for '{target}' must be 'none' or 'read'; "
                f"got {value!r}."
            )

    def to_tmdl(self) -> str:
        """Render the role block per Microsoft TMDL spec (Sept 2025).

        Grammar (from learn.microsoft.com):

            role CategoriesOLS
                modelPermission: read

                tablePermission Customers
                    metadataPermission: none

            role CategoriesOLS
                modelPermission: read

                tablePermission Customers
                    columnPermission Address
                        metadataPermission: none
        """
        lines: List[str] = [f'role "{self.name}" {{']
        if self.model_permission:
            lines.append(f"  modelPermission = {self.model_permission}")
        # Emit each table permission as a nested block.
        for perm in self.table_permissions:
            lines.append(f"  tablePermission {perm.table_name}")
            if perm.filter_expression is not None:
                expr = perm.filter_expression.strip()
                lines.append(f"    filterExpression: ''\n      {expr}\n      ''")
            if perm.metadata_permission is not None:
                lines.append(f"    metadataPermission = {perm.metadata_permission}")
            for col in perm.columns:
                lines.append(f"    columnPermission {col.column_name}")
                lines.append(f"      metadataPermission = {col.metadata_permission}")
        # Legacy / backward-compat rendering: emit the inline form
        # only for entries that aren't already represented as nested
        # ``tablePermission`` blocks. Otherwise we'd produce duplicate
        # rules (one nested, one inline) that Power BI Desktop would
        # reject on save.
        nested_table_names = {
            p.table_name
            for p in self.table_permissions
            if p.metadata_permission is not None
        }
        nested_column_keys = {
            (perm.table_name, col.column_name)
            for perm in self.table_permissions
            for col in perm.columns
        }
        for table in self.hidden_tables:
            if table in nested_table_names:
                continue
            lines.append(f'  "{table}" = metadataPermission = none')
        for table_name, column_name in self.hidden_columns:
            if (table_name, column_name) in nested_column_keys:
                continue
            lines.append(
                f'  "{table_name}" = column "{column_name}" '
                "metadataPermission = none"
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
    r"(?P<header>table|column|measure|relationship|role)\s+("
    r'"(?P<qname>[^"]+)"|'  # double-quoted: "Sales"
    r"'(?P<sname>[^']+)'|"  # single-quoted: 'Time Intelligence'
    r"(?P<name>\S+)"  # bare identifier: Sales
    r")\s*\{",
    re.IGNORECASE | re.DOTALL,
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
        name = match.group("qname") or match.group("sname") or match.group("name") or ""
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
    # Detect a calculation-group table. The grammar is::
    #
    #     table 'Time Intelligence'
    #         calculationGroup
    #             precedence: 10
    #             calculationItem 'YTD' = ...
    #
    # ``calculationGroup`` is a keyword marker (no braces); its children
    # are the ``precedence:`` line and the ``calculationItem 'Name' = …``
    # declarations. They live at the top of the table body, before any
    # ``column`` / ``partition`` / ``measures =`` blocks.
    calc_block = _find_calculation_group_block(body)
    if calc_block is not None:
        table.is_calculation_group = True
        precedence = _match_int(calc_block, "precedence")
        if precedence is not None:
            table.precedence = precedence
        table.calculation_items = _parse_calculation_items(calc_block)
    # Detect a field-parameter table (``isParameterTable`` keyword) and
    # capture the calculated-table partition expression if present.
    if re.search(r"\bisParameterTable\b", body):
        table.is_parameter_table = True
    partitions = _find_partition_blocks(body)
    if partitions:
        table.partitions.extend(partitions)
        # Pull the first calculated-table partition expression onto the
        # table itself so ``to_tmdl`` can re-emit it without round-trip
        # loss.
        for p in partitions:
            if p.get("mode") == "calculated" and "expression" in p:
                table.parameter_partition_expression = p["expression"]
                break
    columns_payload = _find_block(body, "columns")
    if columns_payload:
        table.columns = _parse_columns(columns_payload)
    else:
        # Fallback: look for sibling ``column ... { ... }`` blocks.
        # Used by the calc-group writer which emits columns as siblings
        # of the calculationGroup keyword (per the Microsoft TMDL spec).
        sibling_columns = _parse_sibling_columns(body)
        if sibling_columns:
            table.columns = sibling_columns
    measures_payload = _find_block(body, "measures")
    if measures_payload:
        table.measures = _parse_measures(measures_payload)
    return table


def _find_partition_blocks(body: str) -> List[Dict[str, Any]]:
    """Find ``partition 'Name' = ...`` blocks in the table body.

    Power BI emits two flavours of partition declaration: the older
    M-partition ``"Name" = mode: import, source: { ... }`` form and the
    field-parameter / calculated-table ``"Name" = calculated
    expression = '''...''' form. The calculated-table form spans
    multiple lines because the triple-quoted expression lives on its
    own lines. Both are flattened into a normalised dict so
    :func:`TMDLTable.to_tmdl` can re-render them.
    """
    results: List[Dict[str, Any]] = []
    # First pass: calculated-table partitions (multi-line form).
    for match in re.finditer(
        r'partition\s+(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z_]\w*))' r"\s*=\s*calculated\b",
        body,
    ):
        name = next((g for g in match.groups() if g), "")
        rest = body[match.end() :]
        expr_match = re.search(r"expression\s*=\s*'''(.*?)'''", rest, re.DOTALL)
        entry: Dict[str, Any] = {
            "name": name,
            "mode": "calculated",
        }
        if expr_match:
            entry["expression"] = expr_match.group(1).strip()
        results.append(entry)
    # Second pass: M-partition declarations. These can be either
    # single-line or multi-line (when the M expression spans
    # multiple lines). We use re.DOTALL so ``.`` matches newlines,
    # and a manual brace counter to find the matching ``}`` for the
    # source dict.
    for match in re.finditer(
        r'partition\s+(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z_]\w*))'
        r"\s*=\s*mode:\s*([A-Za-z]+)",
        body,
    ):
        name = next((g for g in match.groups()[:3] if g), "")
        mode = match.group(4)
        rest = body[match.end() :]
        # Skip past any whitespace and ``,`` separator.
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
        source_text: Optional[str] = None
        if rest.startswith("source:"):
            source_start = rest.find("{")
            if source_start != -1:
                # Find the matching closing brace.
                depth = 0
                end_idx = -1
                for idx in range(source_start, len(rest)):
                    ch = rest[idx]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end_idx = idx
                            break
                if end_idx != -1:
                    source_text = rest[source_start : end_idx + 1]
        entry: Dict[str, Any] = {"name": name, "mode": mode}
        if source_text:
            entry["source"] = _parse_source_dict(source_text)
        results.append(entry)
    return results


def _parse_source_dict(text: str) -> Dict[str, Any]:
    """Best-effort parse of the inline ``{ key: value, ... }`` source dict.

    The parser only cares about top-level keys and string / bool
    values. String values are M-style double-quoted with ``""`` as
    the escape sequence, so we split on commas outside of quoted
    regions (regex with re.DOTALL) and unescape embedded ``""``
    back to ``"``.
    """
    # Strip the enclosing braces if present.
    inner = text.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()
    if not inner:
        return {}
    result: Dict[str, Any] = {}
    # Walk char-by-char to honour quoted strings.
    cursor = 0
    while cursor < len(inner):
        # Skip whitespace.
        while cursor < len(inner) and inner[cursor] in " \t\r\n":
            cursor += 1
        if cursor >= len(inner):
            break
        # Read key — until the first ``:`` or ``=`` at top level.
        key_end = cursor
        while key_end < len(inner) and inner[key_end] not in ":=":
            key_end += 1
        if key_end >= len(inner) or inner[key_end] not in ":=":
            break
        key = inner[cursor:key_end].strip()
        cursor = key_end + 1
        # Skip whitespace before value.
        while cursor < len(inner) and inner[cursor] in " \t\r\n":
            cursor += 1
        if cursor >= len(inner):
            break
        # Read value: quoted string OR bare token until comma.
        value: Any
        if inner[cursor] == '"':
            # Quoted string — handle "" as escape for ".
            cursor += 1
            start = cursor
            chars: List[str] = []
            while cursor < len(inner):
                if inner[cursor] == '"':
                    # Look ahead: is this an escaped quote?
                    if cursor + 1 < len(inner) and inner[cursor + 1] == '"':
                        chars.append('"')
                        cursor += 2
                        continue
                    # End of string.
                    break
                chars.append(inner[cursor])
                cursor += 1
            value = "".join(chars)
            cursor += 1  # skip closing "
        else:
            # Bare token up to the next top-level comma.
            start = cursor
            depth = 0
            while cursor < len(inner):
                ch = inner[cursor]
                if ch == "{" or ch == "[":
                    depth += 1
                elif ch == "}" or ch == "]":
                    depth -= 1
                elif ch == "," and depth == 0:
                    break
                cursor += 1
            raw = inner[start:cursor].strip()
            if raw == "true":
                value = True
            elif raw == "false":
                value = False
            else:
                try:
                    value = int(raw)
                except ValueError:
                    value = raw
        # Skip past the ``,`` separator (if any).
        if cursor < len(inner) and inner[cursor] == ",":
            cursor += 1
        result[key] = value
    return result


def _parse_sibling_columns(body: str) -> Dict[str, "TMDLColumn"]:
    """Extract ``column ... { ... }`` blocks appearing as siblings in the body."""
    columns: Dict[str, "TMDLColumn"] = {}
    cursor = 0
    while cursor < len(body):
        match = _SECTION_PATTERN.search(body, cursor)
        if not match:
            break
        if match.group("header").lower() != "column":
            cursor = match.end()
            continue
        col_name = (
            match.group("qname") or match.group("sname") or match.group("name") or ""
        )
        body_start = match.end() - 1
        body_inner, body_end = _extract_balanced(body, body_start)
        if body_inner is None:
            break
        col = _parse_column(col_name, body_inner)
        columns[col.name] = col
        cursor = body_end + 1
    return columns


_CALC_GROUP_KEYWORD = re.compile(r"\bcalculationGroup\b\s*\n", re.IGNORECASE)
_CALC_ITEM_LINE = re.compile(
    r"calculationItem\s+'(?P<name>[^']+)'(?:\s+formatStringDefinition)?\s*=",
    re.IGNORECASE,
)
# Sibling keywords that mark the end of the calculationGroup block.
_CALC_GROUP_END_KEYWORDS = re.compile(
    r"\n\s*(?:column\s+|partition\s+|measures\s*=|annotations\s*=|\S+\s*\{)",
    re.IGNORECASE,
)


def _find_calculation_group_block(body: str) -> Optional[str]:
    """Locate the children of a ``calculationGroup`` keyword inside a table body."""
    match = _CALC_GROUP_KEYWORD.search(body)
    if not match:
        return None
    children_start = match.end()
    end_match = _CALC_GROUP_END_KEYWORDS.search(body, children_start)
    if end_match:
        return body[children_start : end_match.start()].rstrip()
    return body[children_start:].rstrip()


def _match_int(text: str, key: str) -> Optional[int]:
    """Extract an integer from a ``key: N`` or ``key = N`` assignment."""
    pattern = re.compile(rf"\b{re.escape(key)}\s*[=:]\s*(-?\d+)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - the regex only captures digits
        return None


def _parse_calculation_items(calc_block: str) -> List["TMDLCalculationItem"]:
    """Parse ``calculationItem 'X' = ...`` lines out of a calculationGroup block.

    Both the value-form (``calculationItem 'X' = expr``) and the sibling
    format-string-definition form (``calculationItem 'X'
    formatStringDefinition = expr``) are recognised. Items that share a
    name get their ``format_string_definition`` populated from the
    sibling line when present.
    """
    items: Dict[str, "TMDLCalculationItem"] = {}
    cursor = 0
    text = calc_block
    while cursor < len(text):
        line_match = _CALC_ITEM_LINE.search(text, cursor)
        if not line_match:
            break
        item_name = line_match.group("name")
        is_format_def = (
            text[line_match.start() : line_match.end()]
            .lower()
            .endswith("formatstringdefinition =")
        )
        value_start = line_match.end()
        next_item = re.search(
            r"\bcalculationItem\s+'", text[value_start:], re.IGNORECASE
        )
        if next_item:
            value_end = value_start + next_item.start()
            cursor = value_start + next_item.start()
        else:
            value_end = len(text)
            cursor = len(text)
        expr_text = text[value_start:value_end].strip().rstrip(",").strip()
        m_triple = re.match(r"'''(.*?)'''", expr_text, re.DOTALL)
        if m_triple:
            expr_text = m_triple.group(1).strip()
        if is_format_def:
            if item_name in items:
                items[item_name].format_string_definition = expr_text
        else:
            if item_name not in items:
                items[item_name] = TMDLCalculationItem(
                    name=item_name, expression=expr_text
                )
    return list(items.values())


def _parse_columns(block: str) -> Dict[str, TMDLColumn]:
    columns: Dict[str, TMDLColumn] = {}
    cursor = 0
    while cursor < len(block):
        match = _SECTION_PATTERN.search(block, cursor)
        if not match or match.group("header").lower() != "column":
            break
        col_name = (
            match.group("qname") or match.group("sname") or match.group("name") or ""
        )
        body_start = match.end() - 1
        body, body_end = _extract_balanced(block, body_start)
        if body is None:
            break
        cols = _parse_column(col_name, body)
        columns[cols.name] = cols
        cursor = body_end + 1
    return columns


def _parse_column(name: str, body: str) -> TMDLColumn:
    raw = _extract_scalar(body, "dataType") or "string"
    # Always normalise through the same path as the writer so that
    # parsed columns get auto-suggested format strings and any
    # legacy variant spellings (``int32``, ``text``, …) get coerced.
    try:
        data_type = normalize_data_type(raw, on_unknown="fallback")
    except DataTypeError:
        data_type = "string"
    return TMDLColumn(
        name=name,
        data_type=data_type,
        format_string=_extract_scalar(body, "formatString"),
        source_column=_extract_scalar(body, "sourceColumn"),
        sort_by_column=_extract_scalar(body, "sortByColumn"),
        description=_extract_scalar(body, "description"),
        # Marker keywords (no value): present means ``True``.
        is_hidden=bool(re.search(r"\bisHidden\b", body)),
        is_name_inferred=bool(re.search(r"\bisNameInferred\b", body)),
    )


def _parse_measures(block: str) -> Dict[str, TMDLMeasure]:
    measures: Dict[str, TMDLMeasure] = {}
    cursor = 0
    while cursor < len(block):
        match = _SECTION_PATTERN.search(block, cursor)
        if not match or match.group("header").lower() != "measure":
            break
        measure_name = (
            match.group("qname") or match.group("sname") or match.group("name") or ""
        )
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
    # Legacy RLS path: ``tablePermissions = [ "Tbl" = filterExpression: '...' ]``
    block = _find_block(body, "tablePermissions")
    if block:
        # The legacy aggregator format uses an entry of the form
        # ``"Tbl" = filterExpression: '\n  <expr>\n  '``. Parse these
        # inline (no braces) since the brace-based ``_iter_named_blocks``
        # would skip them.
        for perm_name, expr in _iter_legacy_table_permission_entries(block):
            role.table_permissions.append(
                TMDLRolePermission(table_name=perm_name, filter_expression=expr)
            )
    # Microsoft Sept 2025 OLS path: nested ``tablePermission Tbl { ... }``
    # blocks. Each block may carry ``filterExpression`` (RLS),
    # ``metadataPermission`` (OLS whole-table), or nested
    # ``columnPermission`` blocks (OLS column-level).
    for table_name, perm_body in _iter_table_permission_blocks(body):
        # Only look for metadataPermission / filterExpression in the
        # text BEFORE the first columnPermission block — anything
        # inside a columnPermission block belongs to that column, not
        # to the parent tablePermission.
        column_blocks = list(_iter_column_permission_blocks(perm_body))
        # Reconstruct the "pre-column" body by slicing the perm_body
        # from its start up to the start of the first columnPermission
        # match. column_blocks yielded (name, body_after_header) — we
        # need the offset of the header itself. Scan for the header.
        pre_body = perm_body
        if column_blocks:
            first_col_header = _COLUMN_PERMISSION_HEADER.search(perm_body)
            if first_col_header is not None:
                pre_body = perm_body[: first_col_header.start()]
        metadata_permission = _extract_scalar(pre_body, "metadataPermission")
        filter_expression = _extract_expression(pre_body)
        try:
            role.add_table_permission(
                table_name=table_name,
                metadata_permission=metadata_permission,
                filter_expression=filter_expression,
            )
        except TMDLValidationError:
            # If both metadataPermission and filterExpression are
            # present, fall back to keeping the RLS filter and the OLS
            # rule as separate entries — the schema enforces no such
            # overlap, but real-world PBIP files occasionally mix them.
            if filter_expression is not None:
                role.add_table_permission(
                    table_name=table_name, filter_expression=filter_expression
                )
            if metadata_permission is not None:
                role.add_table_permission(
                    table_name=table_name,
                    metadata_permission=metadata_permission,
                )
        for column_name, col_body in column_blocks:
            col_metadata = _extract_scalar(col_body, "metadataPermission")
            if col_metadata is not None:
                role.add_column_permission(
                    table_name=table_name,
                    column_name=column_name,
                    metadata_permission=col_metadata,
                )
    return role


_TABLE_PERMISSION_HEADER = re.compile(
    r"^\s*tablePermission\s+(?P<name>[A-Za-z_][\w]*)\s*$", re.IGNORECASE | re.MULTILINE
)
_COLUMN_PERMISSION_HEADER = re.compile(
    r"^\s*columnPermission\s+(?P<name>[A-Za-z_][\w]*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_LEGACY_TABLE_PERM_ENTRY = re.compile(
    r"\"(?P<name>[^\"]+)\"\s*=\s*filterExpression\s*:\s*\'(?P<expr>.*?)\'",
    re.DOTALL,
)


def _iter_legacy_table_permission_entries(
    block: str,
) -> Iterable[Tuple[str, str]]:
    """Yield ``(table_name, filter_expression)`` for each legacy
    ``tablePermissions = [ ... ]`` aggregator entry.

    The legacy format embeds the table name as a string literal and the
    filter as a single-quoted DAX expression. There are no braces, so
    ``_iter_named_blocks`` can't find them — this regex scans the
    block instead.
    """
    for match in _LEGACY_TABLE_PERM_ENTRY.finditer(block):
        yield match.group("name"), match.group("expr").strip()


def _iter_table_permission_blocks(body: str) -> Iterable[Tuple[str, str]]:
    """Yield ``(table_name, inner_body)`` for each ``tablePermission Tbl``
    block in the role body.

    Each ``tablePermission`` declaration is followed by an indented
    block that may contain ``filterExpression``, ``metadataPermission``,
    or nested ``columnPermission`` declarations. The returned inner
    body is the text between the tablePermission header and the next
    sibling line of equal or lesser indentation.
    """
    matches = list(_TABLE_PERMISSION_HEADER.finditer(body))
    for index, match in enumerate(matches):
        name = match.group("name")
        # The inner body starts on the next line and ends at the next
        # non-indented line or at the start of the next
        # ``tablePermission`` header, whichever comes first.
        start = match.end()
        next_match = matches[index + 1] if index + 1 < len(matches) else None
        end = next_match.start() if next_match is not None else len(body)
        yield name, body[start:end].rstrip()


def _iter_column_permission_blocks(body: str) -> Iterable[Tuple[str, str]]:
    """Yield ``(column_name, inner_body)`` for each ``columnPermission Col``
    block nested inside a tablePermission body."""
    matches = list(_COLUMN_PERMISSION_HEADER.finditer(body))
    for index, match in enumerate(matches):
        name = match.group("name")
        start = match.end()
        next_match = matches[index + 1] if index + 1 < len(matches) else None
        end = next_match.start() if next_match is not None else len(body)
        yield name, body[start:end].rstrip()


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
        name = match.group("qname") or match.group("sname") or match.group("name") or ""
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
    """Extract a triple-quoted expression body, if present.

    Recognises both ``expression = '''…'''`` (TMDL standard) and
    ``filterExpression: ''\n…\n''`` (the older Analysis-Services 1400
    format that Power BI TMDL still emits for RLS row filters).
    """
    match = re.search(r"expression\s*=\s*'''", body)
    if match:
        start = match.end()
        end = body.find("'''", start)
        if end == -1:
            return body[start:].strip()
        return body[start:end].strip()
    # filterExpression: '' ... '' form
    filter_match = re.search(r"filterExpression\s*:\s*'", body)
    if filter_match:
        start = filter_match.end()
        end = body.find("'", start)
        if end == -1:
            return body[start:].strip()
        return body[start:end].strip()
    single = re.search(r"expression\s*=\s*([^\n]+)", body)
    if single:
        return single.group(1).strip()
    return None


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
    # Pre-pass: catch duplicate column names within the SAME column
    # list before any column hits the model. TMDL allows duplicate
    # column names across tables but never within a single table —
    # and Power BI Desktop will refuse to load the model if a table
    # has two columns with the same name. We surface a single
    # combined error so the LLM sees every duplicate at once rather
    # than fixing them one at a time across retries.
    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    for column in columns:
        name = column.get("name") if isinstance(column, dict) else None
        if name is None:
            continue
        seen[name] = seen.get(name, 0) + 1
    duplicates = [n for n, c in seen.items() if c > 1]
    if duplicates:
        raise TMDLValidationError(
            f"Table '{table_name}' declares duplicate column names: "
            f"{sorted(duplicates)}. Power BI Desktop requires column names to "
            f"be unique within a table — rename or merge the duplicates."
        )
    for column in columns:
        if not isinstance(column, dict) or "name" not in column:
            raise TMDLValidationError(
                f"Table '{table_name}' columns must be objects with a 'name' field."
            )
        # Prefer the snake_case spelling the handler / API uses, fall
        # back to the camelCase TMDL spelling if the LLM produced one.
        if "data_type" in column:
            raw = column["data_type"] or "string"
        elif "dataType" in column:
            raw = column["dataType"] or "string"
        else:
            raw = "string"
        # ``normalize_data_type`` accepts aliases (e.g. ``bigint``) and
        # variant spellings (``wholeNumber``) and returns the canonical
        # TMDL form. ``on_unknown="raise"`` makes unrecognised inputs
        # surface a clear error message ("you said 'foo', valid types
        # are …") instead of silently coercing to ``string``.
        try:
            canonical = normalize_data_type(raw, on_unknown="raise")
        except DataTypeError as exc:
            raise TMDLValidationError(
                f"Column '{column.get('name')}' on table '{table_name}' uses "
                f"unsupported dataType: {exc}"
            ) from exc
        # Always write the canonical form into both spellings so the
        # writer and the API stay in sync regardless of which spelling
        # the caller used. This is idempotent when canonical == raw.
        column["data_type"] = canonical
        column["dataType"] = canonical


# Data-type compatibility buckets for relationship endpoints.
#
# Power BI Desktop refuses to load a model whose relationships join
# incompatible column types. We classify each canonical TMDL type
# into one of four buckets — numeric, text, date, boolean — and
# require both endpoints to fall in the same bucket. Aliases map
# to the same buckets.
_TYPE_COMPATIBILITY_BUCKETS = {
    # Numeric family
    "int64": "numeric",
    "wholeNumber": "numeric",
    "decimal": "numeric",
    "decimalNumber": "numeric",
    "currency": "numeric",
    "double": "numeric",
    "percentage": "numeric",
    # Text family
    "string": "text",
    "text": "text",
    # Date/time family
    "dateTime": "date",
    "dateTime64": "date",
    "dateTimeLocal": "date",
    "date": "date",
    "time": "date",
    # Boolean family
    "boolean": "boolean",
    "trueFalse": "boolean",
    # Binary — relationships on binary columns are nonsensical in
    # Power BI; treat as incompatible with anything.
    "binary": "binary",
}


def _types_are_joinable(from_type: str, to_type: str) -> bool:
    """Return True when both relationship endpoints share a bucket.

    Used by :func:`define_relationship_handler` to reject relationships
    that Power BI Desktop would silently refuse to load. The function
    is intentionally permissive within a bucket (any numeric type can
    join any other numeric type) but strict across buckets.
    """
    from_bucket = _TYPE_COMPATIBILITY_BUCKETS.get(from_type)
    to_bucket = _TYPE_COMPATIBILITY_BUCKETS.get(to_type)
    if from_bucket is None or to_bucket is None:
        # Unknown type on either side — let the writer handle that
        # error, but don't fail the relationship here.
        return True
    return from_bucket == to_bucket


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
    """Create or overwrite a semantic relationship between two tables.

    Validates every constraint Power BI Desktop enforces at load time
    so the LLM retry loop learns the right shape rather than
    producing a model that fails to open:

    * Both tables exist in the model.
    * Both columns exist on their respective tables.
    * The column data types are compatible (numeric↔numeric,
      text↔text, date↔date, etc.).
    * The relationship is not self-referential in a way that makes
      the path undefined (e.g. ``A.X → A.X``).
    * An active relationship with the same (fromTable, fromColumn)
      pair doesn't already exist on the model.
    * Cardinality and cross-filter direction are valid TMDL values.

    On the happy path the relationship is appended to the model and
    the model file is re-rendered.
    """
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError(
            "define_relationship handler requires 'model_path' inside context."
        )
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    model = load_model(model_path) if model_path.exists() else TMDLModel()

    # Validate that the tables and columns exist. We surface a single
    # combined error message so the LLM gets a clear picture of
    # what's missing — listing all four checks at once is friendlier
    # than failing on the first one and forcing the caller to retry
    # multiple times.
    errors: List[str] = []
    from_table_obj = model.get_table(from_table)
    to_table_obj = model.get_table(to_table)
    if from_table_obj is None:
        errors.append(f"Table '{from_table}' is not defined in the model.")
    if to_table_obj is None:
        errors.append(f"Table '{to_table}' is not defined in the model.")
    if from_table_obj is not None:
        if from_column not in from_table_obj.columns:
            errors.append(
                f"Column '{from_column}' is not defined on table '{from_table}'. "
                f"Available columns: {sorted(from_table_obj.columns)}."
            )
    if to_table_obj is not None:
        if to_column not in to_table_obj.columns:
            errors.append(
                f"Column '{to_column}' is not defined on table '{to_table}'. "
                f"Available columns: {sorted(to_table_obj.columns)}."
            )
    if errors:
        raise TMDLValidationError("define_relationship: " + " ".join(errors))

    # Type compatibility — Power BI Desktop refuses to load a model
    # whose relationships join incompatible column types. Surface a
    # clear message instead of letting the model silently load and
    # later produce wrong aggregates.
    from_type = from_table_obj.columns[from_column].data_type
    to_type = to_table_obj.columns[to_column].data_type
    if not _types_are_joinable(from_type, to_type):
        raise TMDLValidationError(
            f"Relationship '{from_table}'[{from_column}] ({from_type}) → "
            f"'{to_table}'[{to_column}] ({to_type}) joins incompatible "
            f"data types. Power BI Desktop requires both endpoints to be "
            f"numeric, both text, or both date/time. "
            f"Common aliases: int64↔wholeNumber, decimal↔currency, "
            f"dateTime↔dateTime64↔dateTimeLocal."
        )

    # Reject self-referential relationships in degenerate form.
    if from_table == to_table and from_column == to_column:
        raise TMDLValidationError(
            f"Relationship from '{from_table}'[{from_column}] to itself is "
            f"a degenerate self-join — Power BI rejects it. Pick a different "
            f"column on the same table (e.g. parent_id → id) or a different "
            f"table."
        )

    # Validate cardinality and cross-filter direction.
    valid_cardinalities = {"oneToOne", "oneToMany", "manyToOne", "manyToMany"}
    if cardinality not in valid_cardinalities:
        raise TMDLValidationError(
            f"Relationship cardinality {cardinality!r} is not a known TMDL "
            f"value. Use one of {sorted(valid_cardinalities)}."
        )
    valid_cfd = {"single", "both", "none"}
    if cross_filter_direction not in valid_cfd:
        raise TMDLValidationError(
            f"crossFilterDirection {cross_filter_direction!r} is not a known "
            f"TMDL value. Use one of {sorted(valid_cfd)}."
        )

    # Detect duplicate active relationships with the same (fromTable,
    # fromColumn) pair — only ONE active relationship per from-side
    # endpoint is allowed in Power BI. Inactive duplicates are fine
    # (Power BI uses them as role-playing dimensions).
    if active:
        for existing in model.relationships:
            if (
                existing.is_active
                and existing.from_table == from_table
                and existing.from_column == from_column
            ):
                raise TMDLValidationError(
                    f"An active relationship from '{from_table}'[{from_column}] "
                    f"already exists (named '{existing.name}'). Power BI allows "
                    f"only one active relationship per from-side endpoint. "
                    f"Either deactivate the existing relationship or set "
                    f"active=False on the new one."
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
    group_key: Optional[str] = None,
    table_name: Optional[str] = None,
    precedence: int = 0,
    items: Optional[List[Dict[str, Any]]] = None,
    format_string_definitions: Optional[Dict[str, str]] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    """Materialise a calculation group table.

    Source of items, in priority order:

    1. Explicit ``items`` list passed in by the caller / LLM.
    2. ``DAXCatalog.get_calculation_group(group_key).items`` from the
       project's ``dax_library.json``.

    Each item dict may carry:
      - ``name`` (str, required)
      - ``expression`` (str, DAX; defaults to ``SELECTEDMEASURE()``)
      - ``format_string`` (str, static; optional)
      - ``format_string_definition`` (str, DAX; optional)
      - ``description`` (str, optional)

    The optional ``format_string_definitions`` argument is a name → DAX
    mapping applied to items that don't carry their own
    ``format_string_definition``. Used to pass catalog-level dynamic
    format expressions that the catalog can't represent inline.
    """
    if context is None:
        raise ValueError("add_calculation_group handler requires 'context'.")
    if MODEL_PATH_KEY not in context:
        raise ValueError(
            "add_calculation_group handler requires 'model_path' inside context."
        )
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    chosen_name: Optional[str] = table_name
    catalog_items: List[Dict[str, Any]] = []
    catalog_precedence: Optional[int] = None
    catalog_path = context.get("dax_catalog_path")
    if catalog_path:
        try:
            catalog = DAXCatalog.from_file(str(catalog_path))
        except (FileNotFoundError, ValueError):
            catalog = None
        if catalog is not None and group_key is not None:
            try:
                group = catalog.get_calculation_group(group_key)
            except (KeyError, ValueError):
                group = None
            if group is not None:
                chosen_name = chosen_name or group.table_name
                catalog_items = list(group.items or [])
                catalog_precedence = group.precedence

    if chosen_name is None:
        raise ValueError(
            "add_calculation_group handler could not resolve a table name "
            "from 'table_name' or the DAX catalog group_key "
            f"'{group_key}'."
        )

    effective_precedence = precedence if precedence else (catalog_precedence or 0)
    effective_items = items if items is not None else catalog_items
    if not effective_items:
        raise ValueError(
            "add_calculation_group handler requires at least one item "
            "(either via 'items' or via the DAX catalog group_key)."
        )

    model = load_model(model_path) if model_path.exists() else TMDLModel()
    existing = model.get_table(chosen_name)
    if existing and existing.is_calculation_group:
        return {
            "status": "noop",
            "table": chosen_name,
            "model_path": str(model_path),
            "reason": "calculation group already present",
        }
    if existing and not existing.is_calculation_group:
        raise TMDLValidationError(
            f"Cannot turn table '{chosen_name}' into a calculation group: "
            "it already exists as a regular table. "
            "Pick a different table_name or remove the existing table first."
        )

    table = existing or TMDLTable(name=chosen_name)
    table.mark_calculation_group(precedence=effective_precedence)
    # Required discriminator + ordinal columns. If the caller already
    # added them we leave them alone; otherwise we insert them.
    if "Name" not in table.columns:
        table.add_column(TMDLColumn(name="Name", data_type="string"))
    if "Ordinal" not in table.columns:
        table.add_column(TMDLColumn(name="Ordinal", data_type="wholeNumber"))

    seen_names: set = set()
    for ordinal, item in enumerate(effective_items, start=1):
        if not isinstance(item, dict):
            raise TMDLValidationError(
                f"Calculation item #{ordinal} for group '{chosen_name}' "
                "must be a dict with at least a 'name' field."
            )
        item_name = item.get("name")
        if not item_name:
            raise TMDLValidationError(
                f"Calculation item #{ordinal} for group '{chosen_name}' "
                "is missing a 'name' field."
            )
        if item_name in seen_names:
            raise TMDLValidationError(
                f"Duplicate calculation item name '{item_name}' in "
                f"group '{chosen_name}'."
            )
        seen_names.add(item_name)
        expression = item.get("expression") or "SELECTEDMEASURE()"
        fmt_str = item.get("format_string") or item.get("formatString")
        fmt_def = item.get("format_string_definition")
        if fmt_def is None and format_string_definitions:
            fmt_def = format_string_definitions.get(item_name)
        table.add_calculation_item(
            TMDLCalculationItem(
                name=item_name,
                expression=expression,
                format_string=fmt_str,
                format_string_definition=fmt_def,
                description=item.get("description"),
            )
        )

    if not existing:
        model.add_table(table)
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return {
        "status": "success",
        "table": chosen_name,
        "precedence": effective_precedence,
        "items": [item.name for item in table.calculation_items],
        "model_path": str(model_path),
    }


def _build_field_parameter_expression(
    table_name: str, members: List[Dict[str, Any]]
) -> str:
    """Build the DAX table expression for a field parameter.

    Each member must carry ``display_name`` (the slicer label) and
    ``table_name`` + ``column_name`` (the referenced field). The
    optional ``measure_table`` / ``measure_name`` keys select a measure
    instead of a column. Output is the curly-brace table literal
    Power BI Desktop emits, with one entry per member and the ordinal
    starting at zero in iteration order.
    """
    rendered: List[str] = ["{"]
    for ordinal, member in enumerate(members):
        if not isinstance(member, dict):
            raise TMDLValidationError(
                f"Field parameter member #{ordinal} must be an object "
                "with display_name, table_name, and (column_name | "
                "measure_name)."
            )
        display = member.get("display_name") or member.get("displayName")
        src_table = member.get("table_name") or member.get("tableName")
        if not display or not src_table:
            raise TMDLValidationError(
                f"Field parameter member #{ordinal} is missing "
                "display_name or table_name."
            )
        if member.get("measure_name") or member.get("measureName"):
            measure = member.get("measure_name") or member.get("measureName")
            name_ref = f"'{src_table}'[{measure}]"
        elif member.get("column_name") or member.get("columnName"):
            column = member.get("column_name") or member.get("columnName")
            name_ref = f"'{src_table}'[{column}]"
        else:
            raise TMDLValidationError(
                f"Field parameter member '{display}' is missing "
                "column_name or measure_name."
            )
        rendered.append(f'    ("{display}", NAMEOF({name_ref}), {ordinal}),')
    rendered.append("}")
    return "\n".join(rendered)


def add_field_parameter_handler(
    parameter_name: str,
    members: List[Dict[str, Any]],
    sort_by_column_name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    """Materialise a Power BI field-parameter table.

    Power BI Desktop emits field parameters as calculated tables with
    three columns (Name / Fields / Ordinal) and a DAX table expression
    that uses ``NAMEOF()`` to reference the source field::

        table 'Metric Selection' {
            isParameterTable
            partition 'Metric Selection' = calculated
                expression = '''
                    {
                        ("Revenue",  NAMEOF('Sales'[Revenue]),  0),
                        ("Margin %", NAMEOF('Sales'[Margin %]), 1),
                        ("Units",    NAMEOF('Sales'[Units]),    2)
                    }
                    '''
            column 'Metric Selection' { dataType = string ... isNameInferred }
            column 'Metric Selection Fields' { dataType = string ... isHidden }
            column 'Metric Selection Ordinal' { dataType = int64 ... isHidden }
        }

    ``members`` is a list of dicts. Each dict carries:

      * ``display_name`` (str, required) — the slicer label.
      * ``table_name`` (str, required) — the source table.
      * ``column_name`` (str, one of column/measure required) — the
        source column to reference.
      * ``measure_name`` (str, alternative to column_name) — the source
        measure to reference.

    The handler also adds the conventional Power BI column layout
    (Name / Fields / Ordinal) with the right marker keywords so the
    slicer behaves correctly.
    """
    if not members:
        raise TMDLValidationError("add_field_parameter requires at least one member.")
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError(
            "add_field_parameter handler requires 'model_path' inside context."
        )
    table_name = parameter_name
    chosen_name = table_name
    if not chosen_name:
        raise ValueError("add_field_parameter handler requires 'parameter_name'.")

    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    model = load_model(model_path) if model_path.exists() else TMDLModel()
    existing = model.get_table(chosen_name)
    if existing is not None:
        raise TMDLValidationError(
            f"Cannot create field parameter '{chosen_name}': a table with "
            "that name already exists in the model."
        )

    table = TMDLTable(name=chosen_name)
    table.is_parameter_table = True
    table.parameter_partition_expression = _build_field_parameter_expression(
        chosen_name, members
    )
    # Canonical column names per Microsoft spec.
    fields_col_name = f"{chosen_name} Fields"
    ordinal_col_name = f"{chosen_name} Ordinal"
    sort_target = sort_by_column_name or ordinal_col_name
    table.add_column(
        TMDLColumn(
            name=chosen_name,
            data_type="string",
            is_name_inferred=True,
            source_column=f"{chosen_name}.[Value1]",
            sort_by_column=sort_target,
        )
    )
    table.add_column(
        TMDLColumn(
            name=fields_col_name,
            data_type="string",
            is_hidden=True,
            is_name_inferred=True,
            source_column=f"{chosen_name}.[Value2]",
        )
    )
    table.add_column(
        TMDLColumn(
            name=ordinal_col_name,
            data_type="int64",
            is_hidden=True,
            is_name_inferred=True,
            source_column=f"{chosen_name}.[Value3]",
        )
    )
    model.add_table(table)
    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return {
        "status": "success",
        "parameter": chosen_name,
        "members": [
            {
                "display_name": member.get("display_name") or member.get("displayName"),
                "table_name": member.get("table_name") or member.get("tableName"),
                "column_name": member.get("column_name") or member.get("columnName"),
                "measure_name": member.get("measure_name") or member.get("measureName"),
            }
            for member in members
        ],
        "model_path": str(model_path),
    }


def add_power_query_partition_handler(
    table_name: str,
    *,
    template: Optional[str] = None,
    m_expression: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    partition_name: Optional[str] = None,
    mode: str = "import",
    replace: bool = False,
    promote: bool = False,
    column_types: Optional[List[Dict[str, str]]] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    """Add (or replace) a partition on ``table_name`` whose source is a
    Power Query M expression.

    Two paths:

    * **Template mode** — pass ``template`` (one of ``csv``, ``sql``,
      ``json``, ``sharepoint``, ``odata``, ``web``) and ``params``
      (the keyword arguments the template expects). The handler
      dispatches to the matching builder in
      :mod:`nl2pbip.m_builder`.
    * **Raw M mode** — pass ``m_expression`` (a verbatim M query
      string). The handler validates shape only — no eval.

    The ``promote`` flag wraps the staging query in a Table.PromoteHeaders
    step (the standard Power BI Desktop "Use First Row as Headers"
    pattern). ``column_types`` is an optional list of
    ``{"name": "Id", "type": "Int64.Type"}`` dicts that the promote
    step will apply via Table.TransformColumnTypes.

    Set ``replace=True`` to overwrite an existing partition of the
    same name on this table. Without ``replace``, an attempt to add a
    duplicate partition raises ``TMDLValidationError``.
    """
    from nl2pbip.m_builder import (
        TEMPLATE_BUILDERS,
        build_from_template,
        build_promoted_table,
        validate_m_expression,
    )

    if not context or MODEL_PATH_KEY not in context:
        raise ValueError(
            "add_power_query_partition handler requires 'model_path' " "inside context."
        )
    if not table_name:
        raise ValueError("add_power_query_partition requires a non-empty 'table_name'.")
    if template is None and m_expression is None:
        raise TMDLValidationError(
            "add_power_query_partition requires either 'template' "
            "(with 'params') or 'm_expression'."
        )
    if template is not None and m_expression is not None:
        raise TMDLValidationError(
            "add_power_query_partition accepts 'template' OR "
            "'m_expression', not both."
        )
    if mode not in {"import", "directQuery", "dual", "push"}:
        raise TMDLValidationError(
            f"add_power_query_partition: invalid mode '{mode}'. "
            "Valid: import, directQuery, dual, push."
        )

    # --- Build the M expression --------------------------------------
    if template is not None:
        if template not in TEMPLATE_BUILDERS:
            raise TMDLValidationError(
                f"add_power_query_partition: unknown template "
                f"'{template}'. Valid: {sorted(TEMPLATE_BUILDERS)}."
            )
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise TMDLValidationError(
                "add_power_query_partition: 'params' must be an object "
                f"(got {type(params).__name__})."
            )
        staging = build_from_template(template, params)
    else:
        # m_expression mode — validate shape only.
        assert m_expression is not None  # checked above
        validate_m_expression(m_expression)
        staging = m_expression

    # --- Optionally promote headers / apply column types -------------
    if promote:
        if not column_types:
            column_types = []
        m_text = build_promoted_table(
            staging, table_name=table_name, column_types=column_types
        )
    else:
        m_text = staging

    # --- Persist ------------------------------------------------------
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    if model_path.exists():
        model = load_model(model_path)
    else:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model = TMDLModel()

    table = model.get_table(table_name)
    if table is None:
        raise TMDLValidationError(
            f"Table '{table_name}' does not exist. Use create_table "
            "before adding a Power Query partition to it."
        )

    effective_partition_name = partition_name or table_name
    # If a partition of this name already exists, only overwrite if
    # replace=True. Otherwise surface the conflict.
    existing_idx = None
    for idx, part in enumerate(table.partitions):
        if part.get("name") == effective_partition_name:
            existing_idx = idx
            break
    if existing_idx is not None and not replace:
        raise TMDLValidationError(
            f"Table '{table_name}' already has a partition named "
            f"'{effective_partition_name}'. Pass 'replace=true' to "
            "overwrite it."
        )

    new_partition = {
        "name": effective_partition_name,
        "mode": mode,
        "source": {
            "type": "m",
            "expressionSource": "m",
            "expression": m_text,
        },
    }
    if existing_idx is not None:
        table.partitions[existing_idx] = new_partition
    else:
        table.partitions.append(new_partition)

    model_path.write_text(_render_model_body(model), encoding="utf-8")
    return {
        "status": "success",
        "table": table_name,
        "partition_name": effective_partition_name,
        "mode": mode,
        "template": template,
        "promote": promote,
        "column_types": len(column_types) if column_types else 0,
        "m_expression_bytes": len(m_text.encode("utf-8")),
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
    table_permissions: Optional[List[Dict[str, Any]]] = None,
    column_permissions: Optional[List[Dict[str, Any]]] = None,
    model_permission: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    """Materialise an Object-Level Security (OLS) role.

    Power BI Desktop (Sept 2025) declares OLS rules via nested
    ``tablePermission`` / ``columnPermission`` blocks::

        role CategoriesOLS
            modelPermission = read
            tablePermission Customers
                metadataPermission = none
            tablePermission Customers
                columnPermission Address
                    metadataPermission = none

    Inputs accepted (in priority order):

    * ``table_permissions`` — list of
      ``{table_name, metadata_permission}`` dicts for whole-table OLS.
    * ``column_permissions`` — list of
      ``{table_name, column_name, metadata_permission}`` dicts for
      column-level OLS.
    * ``hidden_tables`` / ``hidden_columns`` (legacy) — same data,
      rendered in the inline ``"Table" = metadataPermission = none``
      form for backward compatibility with code written before this PR.

    At least one OLS rule must be supplied, otherwise
    :class:`TMDLValidationError` is raised.

    ``metadata_permission`` must be ``"none"`` (hide) or ``"read"``
    (allow). Power BI defaults to ``read`` for objects not explicitly
    listed, so this handler only emits rules that differ from the
    default.
    """
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("add_ols_role handler requires 'model_path' inside context.")
    if not any([table_permissions, column_permissions, hidden_tables, hidden_columns]):
        raise TMDLValidationError(
            "add_ols_role requires at least one table permission, "
            "column permission, hidden table, or hidden column."
        )
    role = TMDLRole(name=role_name, model_permission=model_permission)
    if table_permissions:
        for entry in table_permissions:
            if not isinstance(entry, dict):
                raise TMDLValidationError("table_permissions entries must be objects.")
            table_name = entry.get("table_name") or entry.get("tableName")
            if not table_name:
                raise TMDLValidationError(
                    "table_permissions entries must include 'table_name'."
                )
            metadata_permission = entry.get(
                "metadata_permission", entry.get("metadataPermission", "none")
            )
            role.add_table_permission(
                table_name=table_name, metadata_permission=metadata_permission
            )
    if column_permissions:
        for entry in column_permissions:
            if not isinstance(entry, dict):
                raise TMDLValidationError("column_permissions entries must be objects.")
            table_name = entry.get("table_name") or entry.get("tableName")
            column_name = entry.get("column_name") or entry.get("columnName")
            if not table_name or not column_name:
                raise TMDLValidationError(
                    "column_permissions entries must include "
                    "'table_name' and 'column_name'."
                )
            metadata_permission = entry.get(
                "metadata_permission", entry.get("metadataPermission", "none")
            )
            role.add_column_permission(
                table_name=table_name,
                column_name=column_name,
                metadata_permission=metadata_permission,
            )
    if hidden_tables:
        for table_name in hidden_tables:
            role.add_table_permission(table_name=table_name, metadata_permission="none")
            # Mirror into the legacy ``hidden_tables`` list so the
            # handler response still includes the original input
            # representation for callers that read it back.
            role.hidden_tables.append(table_name)
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
            role.add_column_permission(
                table_name=table_name,
                column_name=column_name,
                metadata_permission="none",
            )
            role.hidden_columns.append((table_name, column_name))
    target = _save_role(context, role)
    return {
        "status": "success",
        "role": role_name,
        "table_permissions": [
            {"table_name": p.table_name, "metadata_permission": p.metadata_permission}
            for p in role.table_permissions
            if p.metadata_permission is not None
        ],
        "column_permissions": [
            {
                "table_name": col.table_name,
                "column_name": col.column_name,
                "metadata_permission": col.metadata_permission,
            }
            for perm in role.table_permissions
            for col in perm.columns
        ],
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
    "TMDLCalculationItem",
    "TMDLRelationship",
    "TMDLRole",
    "TMDLRolePermission",
    "TMDLTablePermission",
    "TMDLColumnPermission",
    "VALID_DATA_TYPES",
    "create_table_handler",
    "add_measure_handler",
    "add_pattern_measure_handler",
    "define_relationship_handler",
    "add_calculation_group_handler",
    "add_field_parameter_handler",
    "add_power_query_partition_handler",
    "add_rls_role_handler",
    "add_ols_role_handler",
    "load_model",
    "parse_tmdl_text",
    "roles_workspace_dir",
]
