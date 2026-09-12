"""Tests for the TMDL field-parameter grammar (Microsoft spec).

Field parameters are disconnected calculated tables that drive dynamic
measure / column / table selection via a slicer. Per the spec:

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
        column 'Metric Selection' {
            dataType: string
            sourceColumn: 'Metric Selection.[Value1]'
            sortByColumn: 'Metric Selection Ordinal'
            isNameInferred
        }
        column 'Metric Selection Fields' {
            dataType: string
            sourceColumn: 'Metric Selection.[Value2]'
            isHidden
            isNameInferred
        }
        column 'Metric Selection Ordinal' {
            dataType: int64
            formatString: 0
            sourceColumn: 'Metric Selection.[Value3]'
            isHidden
            isNameInferred
        }
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.tmdl_engine import (
    MODEL_PATH_KEY,
    TMDLColumn,
    TMDLTable,
    TMDLValidationError,
    _build_field_parameter_expression,
    add_field_parameter_handler,
    parse_tmdl_text,
)

# ------------------------------------------------------------------------ ------------------------------------------------------------------------
# _build_field_parameter_expression
# ------------------------------------------------------------------------ ------------------------------------------------------------------------


def test_field_parameter_expression_with_columns() -> None:
    members = [
        {"display_name": "Revenue", "table_name": "Sales", "column_name": "Revenue"},
        {"display_name": "Units", "table_name": "Sales", "column_name": "Units"},
    ]
    expr = _build_field_parameter_expression("Metric Selection", members)
    assert expr.startswith("{")
    assert expr.endswith("}")
    assert "NAMEOF('Sales'[Revenue])" in expr
    assert "Revenue" in expr
    # Ordinals start at zero.
    assert ", 0)," in expr
    assert ", 1)," in expr


def test_field_parameter_expression_with_measures() -> None:
    members = [
        {
            "display_name": "Total Revenue",
            "table_name": "Sales",
            "measure_name": "Total Revenue",
        },
    ]
    expr = _build_field_parameter_expression("M", members)
    assert "NAMEOF('Sales'[Total Revenue])" in expr


def test_field_parameter_expression_rejects_missing_display_name() -> None:
    members = [{"table_name": "Sales", "column_name": "Revenue"}]
    with pytest.raises(TMDLValidationError) as exc_info:
        _build_field_parameter_expression("X", members)
    assert "display_name" in str(exc_info.value)


def test_field_parameter_expression_rejects_missing_table_name() -> None:
    members = [{"display_name": "Revenue", "column_name": "Revenue"}]
    with pytest.raises(TMDLValidationError) as exc_info:
        _build_field_parameter_expression("X", members)
    assert "table_name" in str(exc_info.value)


def test_field_parameter_expression_rejects_neither_column_nor_measure() -> None:
    members = [{"display_name": "Revenue", "table_name": "Sales"}]
    with pytest.raises(TMDLValidationError) as exc_info:
        _build_field_parameter_expression("X", members)
    assert "column_name or measure_name" in str(exc_info.value)


def test_field_parameter_expression_accepts_camelcase() -> None:
    members = [
        {
            "displayName": "Total Revenue",
            "tableName": "Sales",
            "measureName": "Total Revenue",
        }
    ]
    expr = _build_field_parameter_expression("M", members)
    assert "NAMEOF('Sales'[Total Revenue])" in expr


def test_field_parameter_expression_rejects_non_dict_member() -> None:
    with pytest.raises(TMDLValidationError):
        _build_field_parameter_expression("X", ["bad"])  # type: ignore[list-item]


# ------------------------------------------------------------------------ ------------------------------------------------------------------------
# TMDLTable rendering
# ------------------------------------------------------------------------ ------------------------------------------------------------------------


def _build_field_parameter_table() -> TMDLTable:
    table = TMDLTable(name="Metric Selection")
    table.is_parameter_table = True
    table.parameter_partition_expression = (
        "{\n    (\"Revenue\", NAMEOF('Sales'[Revenue]), 0)\n}"
    )
    table.add_column(
        TMDLColumn(
            name="Metric Selection",
            data_type="string",
            is_name_inferred=True,
            source_column="Metric Selection.[Value1]",
            sort_by_column="Metric Selection Ordinal",
        )
    )
    table.add_column(
        TMDLColumn(
            name="Metric Selection Fields",
            data_type="string",
            is_hidden=True,
            is_name_inferred=True,
            source_column="Metric Selection.[Value2]",
        )
    )
    table.add_column(
        TMDLColumn(
            name="Metric Selection Ordinal",
            data_type="int64",
            is_hidden=True,
            is_name_inferred=True,
            source_column="Metric Selection.[Value3]",
        )
    )
    return table


def test_field_parameter_table_emits_isparametertable_marker() -> None:
    out = _build_field_parameter_table().to_tmdl()
    assert "  isParameterTable" in out
    assert 'partition "Metric Selection" = calculated' in out


def test_field_parameter_table_emits_namename_inferred_on_name_column() -> None:
    out = _build_field_parameter_table().to_tmdl()
    name_col = out.split('column "Metric Selection"')[1].split("}")[0]
    assert "isNameInferred" in name_col


def test_field_parameter_table_emits_ishidden_on_fields_column() -> None:
    out = _build_field_parameter_table().to_tmdl()
    fields_col = out.split('column "Metric Selection Fields"')[1].split("}")[0]
    assert "isHidden" in fields_col


def test_field_parameter_table_emits_ishidden_on_ordinal_column() -> None:
    out = _build_field_parameter_table().to_tmdl()
    ordinal_col = out.split('column "Metric Selection Ordinal"')[1].split("}")[0]
    assert "isHidden" in ordinal_col


def test_field_parameter_table_emits_sortbycolumn_on_name_column() -> None:
    out = _build_field_parameter_table().to_tmdl()
    name_col = out.split('column "Metric Selection"')[1].split("}")[0]
    assert 'sortByColumn = "Metric Selection Ordinal"' in name_col


def test_field_parameter_table_does_not_emit_partition_without_expression() -> None:
    table = TMDLTable(name="Empty")
    table.is_parameter_table = True
    # No parameter_partition_expression set
    out = table.to_tmdl()
    assert "isParameterTable" in out
    assert "partition" not in out


# ------------------------------------------------------------------------ ------------------------------------------------------------------------
# Parser round-trip
# ------------------------------------------------------------------------ ------------------------------------------------------------------------


def test_parser_round_trips_isparametertable_marker() -> None:
    src = _build_field_parameter_table().to_tmdl()
    model = parse_tmdl_text(src)
    table = model.get_table("Metric Selection")
    assert table.is_parameter_table is True
    assert table.parameter_partition_expression is not None
    assert "NAMEOF('Sales'[Revenue])" in table.parameter_partition_expression


def test_parser_round_trips_column_markers() -> None:
    src = _build_field_parameter_table().to_tmdl()
    model = parse_tmdl_text(src)
    table = model.get_table("Metric Selection")
    name_col = table.columns["Metric Selection"]
    assert name_col.is_name_inferred is True
    assert name_col.is_hidden is False
    assert name_col.sort_by_column == "Metric Selection Ordinal"
    fields_col = table.columns["Metric Selection Fields"]
    assert fields_col.is_hidden is True
    assert fields_col.is_name_inferred is True
    ordinal_col = table.columns["Metric Selection Ordinal"]
    assert ordinal_col.is_hidden is True
    assert ordinal_col.data_type == "int64"


def test_parser_reads_calculated_partition_expression() -> None:
    src = _build_field_parameter_table().to_tmdl()
    model = parse_tmdl_text(src)
    table = model.get_table("Metric Selection")
    # The first partition in the rendered output is the calculated one.
    assert any(p.get("mode") == "calculated" for p in table.partitions)


def test_parser_does_not_treat_regular_table_as_parameter() -> None:
    text = """
        table Sales {
            columns = [
                column Amount
                    dataType = decimal
            ]
        }
    """
    model = parse_tmdl_text(text)
    sales = model.get_table("Sales")
    assert sales.is_parameter_table is False


# ------------------------------------------------------------------------ ------------------------------------------------------------------------
# Handler tests
# ------------------------------------------------------------------------ ------------------------------------------------------------------------


@pytest.fixture()
def model_path(tmp_path: Path) -> Path:
    p = tmp_path / "model.tmdl"
    p.write_text("", encoding="utf-8")
    return p


def test_handler_creates_field_parameter_table(
    tmp_path: Path, model_path: Path
) -> None:
    result = add_field_parameter_handler(
        parameter_name="Metric Selection",
        members=[
            {
                "display_name": "Total Revenue",
                "table_name": "Sales",
                "measure_name": "Total Revenue",
            },
            {
                "display_name": "Region",
                "table_name": "Sales",
                "column_name": "Region",
            },
        ],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    assert result["parameter"] == "Metric Selection"
    assert len(result["members"]) == 2
    # File persisted.
    written = (tmp_path / "model.tmdl").read_text(encoding="utf-8")
    assert "isParameterTable" in written
    assert "NAMEOF('Sales'[Total Revenue])" in written
    assert "NAMEOF('Sales'[Region])" in written
    # All three canonical columns present.
    assert 'column "Metric Selection"' in written
    assert 'column "Metric Selection Fields"' in written
    assert 'column "Metric Selection Ordinal"' in written


def test_handler_rejects_empty_members(tmp_path: Path, model_path: Path) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_field_parameter_handler(
            parameter_name="X",
            members=[],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "at least one member" in str(exc_info.value)


def test_handler_rejects_missing_display_name(tmp_path: Path, model_path: Path) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_field_parameter_handler(
            parameter_name="X",
            members=[{"table_name": "Sales", "column_name": "Revenue"}],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "display_name" in str(exc_info.value)


def test_handler_rejects_missing_table_name(tmp_path: Path, model_path: Path) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_field_parameter_handler(
            parameter_name="X",
            members=[{"display_name": "Revenue", "column_name": "Revenue"}],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "table_name" in str(exc_info.value)


def test_handler_rejects_missing_both_column_and_measure(
    tmp_path: Path, model_path: Path
) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_field_parameter_handler(
            parameter_name="X",
            members=[{"display_name": "Revenue", "table_name": "Sales"}],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "column_name or measure_name" in str(exc_info.value)


def test_handler_rejects_existing_table_with_same_name(
    tmp_path: Path, model_path: Path
) -> None:
    # First create a table with the same name as the parameter.
    add_field_parameter_handler(
        parameter_name="Metric Selection",
        members=[
            {
                "display_name": "Revenue",
                "table_name": "Sales",
                "column_name": "Revenue",
            }
        ],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    # Second call must fail.
    with pytest.raises(TMDLValidationError) as exc_info:
        add_field_parameter_handler(
            parameter_name="Metric Selection",
            members=[
                {
                    "display_name": "Region",
                    "table_name": "Sales",
                    "column_name": "Region",
                }
            ],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "already exists" in str(exc_info.value)


def test_handler_requires_model_path() -> None:
    with pytest.raises(ValueError) as exc_info:
        add_field_parameter_handler(
            parameter_name="X",
            members=[{"display_name": "Y", "table_name": "T", "column_name": "C"}],
            context={},
        )
    assert "model_path" in str(exc_info.value)


def test_handler_accepts_camelcase_keys(tmp_path: Path, model_path: Path) -> None:
    result = add_field_parameter_handler(
        parameter_name="M",
        members=[
            {
                "displayName": "Revenue",
                "tableName": "Sales",
                "measureName": "Total Revenue",
            }
        ],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    assert result["members"][0]["display_name"] == "Revenue"


def test_handler_supports_custom_sort_by_column(
    tmp_path: Path, model_path: Path
) -> None:
    add_field_parameter_handler(
        parameter_name="M",
        members=[
            {
                "display_name": "Revenue",
                "table_name": "Sales",
                "column_name": "Revenue",
            }
        ],
        sort_by_column_name="Custom Ordinal",
        context={MODEL_PATH_KEY: str(model_path)},
    )
    written = (tmp_path / "model.tmdl").read_text(encoding="utf-8")
    name_col = written.split('column "M"')[1].split("}")[0]
    assert 'sortByColumn = "Custom Ordinal"' in name_col


def test_handler_rejects_non_dict_member(tmp_path: Path, model_path: Path) -> None:
    with pytest.raises(TMDLValidationError):
        add_field_parameter_handler(
            parameter_name="X",
            members=["bad"],  # type: ignore[list-item]
            context={MODEL_PATH_KEY: str(model_path)},
        )
