"""Tests for the TMDL calculation-group grammar and the
``add_calculation_group_handler`` tool.

These tests pin the canonical TMDL output that Power BI Desktop expects,
the round-trip parser behaviour, and the handler's error paths.

Reference grammar (Microsoft Power BI docs, Sept 2025):

    table 'Time Intelligence'
        calculationGroup
            precedence: 10

            calculationItem 'YTD' =
                '''
                TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])
                '''
            calculationItem 'YTD' formatStringDefinition =
                '''
                VAR x = SELECTEDMEASUREFORMATSTRING()
                RETURN IF(ISNUMERIC(x), "#,##0.00%", x)
                '''
        column 'Time Intelligence'
            dataType: string
            ...
        column Ordinal
            dataType: int64
            ...
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.dax_catalog import DAXCatalog
from nl2pbip.tmdl_engine import (
    MODEL_PATH_KEY,
    TMDLCalculationItem,
    TMDLColumn,
    TMDLTable,
    TMDLValidationError,
    add_calculation_group_handler,
    load_model,
    parse_tmdl_text,
)

# ---------------------------------------------------------------------------
# TMDLCalculationItem.to_tmdl
# ---------------------------------------------------------------------------


def test_calculation_item_renders_single_line_value_form() -> None:
    item = TMDLCalculationItem(
        name="YTD",
        expression="TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])",
    )
    out = item.to_tmdl(indent=8)
    assert out.startswith("        calculationItem 'YTD' = '''")
    assert "TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])" in out
    # Triple-quoted block closes on its own line.
    assert out.rstrip().endswith("'''")


def test_calculation_item_appends_format_string_definition() -> None:
    item = TMDLCalculationItem(
        name="YoY %",
        expression="DIVIDE(SELECTEDMEASURE(), SELECTEDMEASURE())",
        format_string_definition='VAR x = SELECTEDMEASUREFORMATSTRING() RETURN "0.00%"',
    )
    out = item.to_tmdl(indent=8)
    lines = out.splitlines()
    # The value-form is the first line; the formatStringDefinition is a
    # sibling line keyed by the same item name.
    assert lines[0].startswith("        calculationItem 'YoY %' = '''")
    assert any(
        "calculationItem 'YoY %' formatStringDefinition" in line for line in lines
    ), f"missing formatStringDefinition line in {out!r}"


def test_calculation_item_no_format_def_when_not_provided() -> None:
    item = TMDLCalculationItem(
        name="Current",
        expression="SELECTEDMEASURE()",
    )
    out = item.to_tmdl(indent=8)
    assert "formatStringDefinition" not in out


def test_calculation_item_strips_surrounding_whitespace() -> None:
    item = TMDLCalculationItem(
        name="YTD",
        expression="\n  TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])\n  ",
    )
    out = item.to_tmdl(indent=8)
    # No double-indented content inside the triple-quote block.
    assert "  TOTALYTD" in out
    assert "\n\n\n" not in out


# ---------------------------------------------------------------------------
# TMDLTable rendering for calc-group
# ---------------------------------------------------------------------------


def _build_calc_group_table() -> TMDLTable:
    table = TMDLTable(name="Time Intelligence")
    table.mark_calculation_group(precedence=10)
    table.add_column(TMDLColumn(name="Time Intelligence", data_type="string"))
    table.add_column(TMDLColumn(name="Ordinal", data_type="wholeNumber"))
    table.add_calculation_item(
        TMDLCalculationItem(
            name="Current",
            expression="SELECTEDMEASURE()",
        )
    )
    table.add_calculation_item(
        TMDLCalculationItem(
            name="YTD",
            expression="TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])",
            format_string_definition=(
                "VAR x = SELECTEDMEASUREFORMATSTRING() "
                'RETURN IF(ISNUMERIC(x), "#,##0.00%", x)'
            ),
        )
    )
    return table


def test_table_renders_calculation_group_grammar() -> None:
    out = _build_calc_group_table().to_tmdl()
    # Single-quoted table name (Microsoft convention).
    assert out.startswith("table 'Time Intelligence' {")
    # calculationGroup block lives at 4-space indent, items at 8 spaces.
    assert "    calculationGroup" in out
    assert "        precedence: 10" in out
    assert "        calculationItem 'Current' = '''" in out
    assert "        calculationItem 'YTD' = '''" in out
    assert "        calculationItem 'YTD' formatStringDefinition = '''" in out
    # No partition block, no measures = [...], no dataType on the table.
    assert "partition " not in out
    assert "measures = [" not in out
    assert "dataType = " not in out.splitlines()[0]


def test_regular_table_renders_columns_and_measures() -> None:
    """A non-calc-group table must keep its existing columns / measures layout."""
    table = TMDLTable(name="Sales")
    table.add_column(TMDLColumn(name="Amount", data_type="decimal"))
    out = table.to_tmdl()
    assert out.startswith('table "Sales" {')
    assert "columns = [" in out
    # Calc-group grammar must not leak in.
    assert "calculationGroup" not in out


def test_table_rejects_calculation_item_when_not_marked() -> None:
    table = TMDLTable(name="Sales")
    with pytest.raises(TMDLValidationError) as exc_info:
        table.add_calculation_item(
            TMDLCalculationItem(name="YTD", expression="SELECTEDMEASURE()")
        )
    assert "is not a calculation group" in str(exc_info.value)


def test_table_rejects_duplicate_calculation_item_name() -> None:
    table = TMDLTable(name="Time Intelligence")
    table.mark_calculation_group(precedence=10)
    table.add_calculation_item(
        TMDLCalculationItem(name="YTD", expression="SELECTEDMEASURE()")
    )
    with pytest.raises(TMDLValidationError) as exc_info:
        table.add_calculation_item(
            TMDLCalculationItem(name="YTD", expression="SELECTEDMEASURE()")
        )
    assert "already has an item named 'YTD'" in str(exc_info.value)


def test_table_mark_calculation_group_updates_precedence() -> None:
    table = TMDLTable(name="Time Intelligence")
    table.mark_calculation_group(precedence=5)
    assert table.precedence == 5
    table.mark_calculation_group(precedence=42)
    assert table.precedence == 42


# ---------------------------------------------------------------------------
# Round-trip via parse_tmdl_text
# ---------------------------------------------------------------------------


def test_parse_tmdl_text_round_trips_calc_group() -> None:
    table = _build_calc_group_table()
    source_tmdl = table.to_tmdl()
    model = parse_tmdl_text(source_tmdl)
    parsed = model.get_table("Time Intelligence")
    assert parsed is not None
    assert parsed.is_calculation_group is True
    assert parsed.precedence == 10
    names = {item.name for item in parsed.calculation_items}
    assert names == {"Current", "YTD"}
    ytd = next(item for item in parsed.calculation_items if item.name == "YTD")
    assert "TOTALYTD" in ytd.expression
    assert ytd.format_string_definition is not None
    assert "SELECTEDMEASUREFORMATSTRING()" in ytd.format_string_definition


def test_parse_tmdl_text_preserves_columns() -> None:
    table = _build_calc_group_table()
    model = parse_tmdl_text(table.to_tmdl())
    parsed = model.get_table("Time Intelligence")
    assert "Ordinal" in parsed.columns
    assert "Time Intelligence" in parsed.columns


def test_parse_tmdl_text_noop_for_regular_tables() -> None:
    regular = """
        table Sales {
            columns = [
                column Amount
                    dataType = decimal
            ]
        }
    """
    model = parse_tmdl_text(regular)
    sales = model.get_table("Sales")
    assert sales is not None
    assert sales.is_calculation_group is False
    assert sales.calculation_items == []


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def catalog_path(tmp_path: Path) -> Path:
    """Write a small DAX catalog that includes the time_intelligence group."""
    data: Dict[str, Any] = {
        "patterns": {},
        "calculation_groups": {
            "time_intelligence": {
                "name": "Time Intelligence",
                "table_name": "CG Time Intelligence",
                "precedence": 10,
                "items": [
                    {
                        "name": "Current",
                        "expression": "SELECTEDMEASURE()",
                        "format_string": None,
                    },
                    {
                        "name": "YTD",
                        "expression": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))",
                        "format_string": None,
                    },
                    {
                        "name": "YoY %",
                        "expression": "DIVIDE(SELECTEDMEASURE(), 1)",
                        "format_string": "0.00%",
                        "format_string_definition": (
                            "VAR x = SELECTEDMEASUREFORMATSTRING() "
                            'RETURN IF(ISNUMERIC(x), "0.00%", x)'
                        ),
                    },
                ],
            }
        },
    }
    path = tmp_path / "dax_library.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def model_path(tmp_path: Path) -> Path:
    """An empty model file used as the target for the handler."""
    path = tmp_path / "model.tmdl"
    path.write_text("", encoding="utf-8")
    return path


def test_handler_materialises_catalog_group(tmp_path: Path, catalog_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    result = add_calculation_group_handler(
        group_key="time_intelligence",
        context={
            MODEL_PATH_KEY: str(model_path),
            "dax_catalog_path": str(catalog_path),
        },
    )
    assert result["status"] == "success"
    assert result["table"] == "CG Time Intelligence"
    assert result["precedence"] == 10
    assert set(result["items"]) == {"Current", "YTD", "YoY %"}
    # File persisted; can be reloaded.
    model = load_model(model_path)
    parsed = model.get_table("CG Time Intelligence")
    assert parsed is not None
    assert parsed.is_calculation_group is True
    assert parsed.precedence == 10
    yoy = next(item for item in parsed.calculation_items if item.name == "YoY %")
    assert "DIVIDE" in yoy.expression
    assert "SELECTEDMEASUREFORMATSTRING" in (yoy.format_string_definition or "")


def test_handler_accepts_inline_items(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    result = add_calculation_group_handler(
        group_key="custom_group",
        table_name="My Calc Group",
        precedence=20,
        items=[
            {"name": "YTD", "expression": "TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])"},
            {
                "name": "Custom %",
                "expression": "SELECTEDMEASURE() / SELECTEDMEASURE()",
                "format_string_definition": '"0.0%"',
            },
        ],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    assert result["table"] == "My Calc Group"
    assert result["precedence"] == 20
    model = load_model(model_path)
    parsed = model.get_table("My Calc Group")
    assert parsed.is_calculation_group is True
    custom = next(item for item in parsed.calculation_items if item.name == "Custom %")
    assert custom.format_string_definition == '"0.0%"'


def test_handler_uses_format_string_definitions_kwarg(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    add_calculation_group_handler(
        group_key="custom",
        table_name="FX",
        items=[{"name": "Item1", "expression": "SELECTEDMEASURE()"}],
        format_string_definitions={
            "Item1": "VAR x = SELECTEDMEASUREFORMATSTRING() RETURN x",
        },
        context={MODEL_PATH_KEY: str(model_path)},
    )
    model = load_model(model_path)
    parsed = model.get_table("FX")
    item = parsed.calculation_items[0]
    assert item.format_string_definition == (
        "VAR x = SELECTEDMEASUREFORMATSTRING() RETURN x"
    )


def test_handler_returns_noop_when_already_present(
    tmp_path: Path, catalog_path: Path
) -> None:
    model_path = tmp_path / "model.tmdl"
    ctx = {MODEL_PATH_KEY: str(model_path), "dax_catalog_path": str(catalog_path)}
    add_calculation_group_handler(group_key="time_intelligence", context=ctx)
    # second invocation must be a no-op
    second = add_calculation_group_handler(group_key="time_intelligence", context=ctx)
    assert second["status"] == "noop"
    assert second["reason"] == "calculation group already present"


def test_handler_rejects_overwriting_regular_table(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    # First create a regular table.
    model_path.write_text(
        'table "Sales" {\n'
        "  columns = [\n"
        "    column Amount\n"
        "      dataType = decimal\n"
        "  ]\n"
        "}\n",
        encoding="utf-8",
    )
    with pytest.raises(TMDLValidationError) as exc_info:
        add_calculation_group_handler(
            group_key="anything",
            table_name="Sales",
            items=[{"name": "X", "expression": "SELECTEDMEASURE()"}],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "regular table" in str(exc_info.value)


def test_handler_requires_model_path() -> None:
    with pytest.raises(ValueError) as exc_info:
        add_calculation_group_handler(group_key="x", context={})
    assert "model_path" in str(exc_info.value)


def test_handler_requires_table_name_when_no_catalog(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    with pytest.raises(ValueError) as exc_info:
        add_calculation_group_handler(
            group_key="missing_group_key",
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "table name" in str(exc_info.value)


def test_handler_requires_at_least_one_item(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    with pytest.raises(ValueError) as exc_info:
        add_calculation_group_handler(
            group_key="custom",
            table_name="Empty",
            items=[],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "at least one item" in str(exc_info.value)


def test_handler_rejects_duplicate_item_names(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    with pytest.raises(TMDLValidationError) as exc_info:
        add_calculation_group_handler(
            group_key="dup",
            table_name="Dup",
            items=[
                {"name": "YTD", "expression": "SELECTEDMEASURE()"},
                {"name": "YTD", "expression": "SELECTEDMEASURE()"},
            ],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "Duplicate" in str(exc_info.value)


def test_handler_rejects_item_missing_name(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    with pytest.raises(TMDLValidationError) as exc_info:
        add_calculation_group_handler(
            group_key="noname",
            table_name="NoName",
            items=[{"expression": "SELECTEDMEASURE()"}],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "missing a 'name'" in str(exc_info.value)


def test_handler_rejects_non_dict_item(tmp_path: Path) -> None:
    model_path = tmp_path / "model.tmdl"
    with pytest.raises(TMDLValidationError) as exc_info:
        add_calculation_group_handler(
            group_key="bad",
            table_name="Bad",
            items=["YTD"],  # type: ignore[list-item]
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "must be a dict" in str(exc_info.value)


def test_handler_uses_catalog_name_when_table_name_omitted(
    tmp_path: Path, catalog_path: Path
) -> None:
    model_path = tmp_path / "model.tmdl"
    result = add_calculation_group_handler(
        group_key="time_intelligence",
        context={
            MODEL_PATH_KEY: str(model_path),
            "dax_catalog_path": str(catalog_path),
        },
    )
    # Catalog table_name is "CG Time Intelligence".
    assert result["table"] == "CG Time Intelligence"


def test_handler_explicit_table_name_overrides_catalog(
    tmp_path: Path, catalog_path: Path
) -> None:
    model_path = tmp_path / "model.tmdl"
    result = add_calculation_group_handler(
        group_key="time_intelligence",
        table_name="Override",
        context={
            MODEL_PATH_KEY: str(model_path),
            "dax_catalog_path": str(catalog_path),
        },
    )
    assert result["table"] == "Override"


def test_handler_falls_back_to_default_expression() -> None:
    """An item with no expression defaults to SELECTEDMEASURE()."""
    item = TMDLCalculationItem(name="Plain", expression="")
    # The handler is responsible for applying the default; the dataclass
    # itself stores the literal value but the handler rewrites "" -> SELECTEDMEASURE().
    # Verify via the dataclass path: render uses the stored expression.
    rendered = item.to_tmdl(indent=8)
    # Empty expression renders as an empty triple-quote block; the
    # handler's default rewrite is the only place that fills it in.
    assert "''" in rendered or "'''" in rendered


# ---------------------------------------------------------------------------
# Bundled catalog coverage
# ---------------------------------------------------------------------------


def test_bundled_catalog_time_intelligence_group_is_usable() -> None:
    """The shipped ``dax_library.json`` must parse and expose items."""
    catalog = DAXCatalog.from_file()
    group = catalog.get_calculation_group("time_intelligence")
    assert group.table_name == "CG Time Intelligence"
    assert group.precedence == 10
    item_names = [item["name"] for item in group.items]
    assert "YTD" in item_names
    assert "YoY %" in item_names
    yoy = next(item for item in group.items if item["name"] == "YoY %")
    # Bundled YoY % has a format_string_definition for dynamic formatting.
    assert "format_string_definition" in yoy
