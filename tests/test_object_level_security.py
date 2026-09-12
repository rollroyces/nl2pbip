"""Tests for the TMDL Object-Level Security (OLS) grammar.

OLS rules hide entire tables or specific columns from a Power BI role
based on the role's membership. Per Microsoft spec (Sept 2025):

    role CategoriesOLS
        modelPermission = read
        tablePermission Customers
            metadataPermission = none
        tablePermission Customers
            columnPermission Address
                metadataPermission = none

This test module pins the canonical TMDL output, the parser round-trip,
and the handler's error paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from nl2pbip.tmdl_engine import (
    MODEL_PATH_KEY,
    TMDLColumnPermission,
    TMDLRole,
    TMDLTablePermission,
    TMDLValidationError,
    add_ols_role_handler,
    parse_tmdl_text,
)

# ---------------------------------------------------------------------------
# TMDLRole / TMDLTablePermission / TMDLColumnPermission to_tmdl
# ---------------------------------------------------------------------------


def test_table_level_ols_emits_nested_tablepermission_block() -> None:
    role = TMDLRole(name="CategoriesOLS", model_permission="read")
    role.add_table_permission(table_name="Customers", metadata_permission="none")
    out = role.to_tmdl()
    assert out.startswith('role "CategoriesOLS" {')
    assert "  modelPermission = read" in out
    # Nested tablePermission block, no inline "Table" = metadataPermission line.
    assert "  tablePermission Customers" in out
    assert "    metadataPermission = none" in out
    assert '"Customers" = metadataPermission' not in out


def test_column_level_ols_emits_nested_columnpermission_block() -> None:
    role = TMDLRole(name="SalesPublic")
    role.add_column_permission(
        table_name="Sales", column_name="SaleId", metadata_permission="none"
    )
    out = role.to_tmdl()
    assert 'role "SalesPublic" {' in out
    assert "  tablePermission Sales" in out
    assert "    columnPermission SaleId" in out
    assert "      metadataPermission = none" in out


def test_combined_rls_and_ols_per_table_raises() -> None:
    """A single tablePermission block can't carry both filterExpression
    and metadataPermission per Microsoft spec."""
    role = TMDLRole(name="Bad")
    with pytest.raises(TMDLValidationError) as exc_info:
        role.add_table_permission(
            table_name="Sales",
            filter_expression="[Region] = 'US'",
            metadata_permission="none",
        )
    assert "cannot carry both" in str(exc_info.value)


def test_combined_rls_and_ols_split_into_two_tablepermissions() -> None:
    role = TMDLRole(name="Split")
    role.add_table_permission(table_name="Sales", filter_expression="[Region] = 'US'")
    role.add_table_permission(table_name="Sales", metadata_permission="none")
    out = role.to_tmdl()
    # Two tablePermission blocks (RLS first, then OLS).
    assert out.count("  tablePermission Sales") == 2
    assert "    filterExpression" in out
    assert "    metadataPermission = none" in out


def test_invalid_metadata_permission_value_raises() -> None:
    role = TMDLRole(name="Bad")
    with pytest.raises(TMDLValidationError) as exc_info:
        role.add_table_permission(table_name="Sales", metadata_permission="hidden")
    assert "must be 'none' or 'read'" in str(exc_info.value)


def test_add_column_permission_to_existing_table_permission() -> None:
    """Calling add_column_permission on a table that already has a
    metadataPermission rule appends to the same tablePermission block
    rather than emitting two siblings."""
    role = TMDLRole(name="Combo")
    role.add_table_permission(table_name="Customers", metadata_permission="none")
    role.add_column_permission(
        table_name="Customers", column_name="Email", metadata_permission="none"
    )
    out = role.to_tmdl()
    # Only one tablePermission Customers block.
    assert out.count("  tablePermission Customers") == 1
    # metadataPermission + columnPermission both under it.
    assert "    metadataPermission = none" in out
    assert "    columnPermission Email" in out
    assert "      metadataPermission = none" in out


def test_existing_column_permission_is_updated_in_place() -> None:
    role = TMDLRole(name="Updatable")
    role.add_column_permission(
        table_name="Customers", column_name="Email", metadata_permission="none"
    )
    role.add_column_permission(
        table_name="Customers", column_name="Email", metadata_permission="read"
    )
    out = role.to_tmdl()
    # Only one columnPermission block for Email.
    assert out.count("columnPermission Email") == 1
    # The latest value wins.
    assert "      metadataPermission = read" in out
    assert "      metadataPermission = none" not in out


def test_legacy_hidden_tables_still_emit_inline_form() -> None:
    """Backward compatibility: hidden_tables emits the inline
    "Table" = metadataPermission = none form so older Power BI Desktop
    versions still open the file."""
    role = TMDLRole(name="Legacy", hidden_tables=["Customers"])
    out = role.to_tmdl()
    assert '"Customers" = metadataPermission = none' in out


def test_legacy_hidden_columns_still_emit_inline_form() -> None:
    role = TMDLRole(name="Legacy", hidden_columns=[("Customers", "Email")])
    out = role.to_tmdl()
    assert '"Customers" = column "Email" metadataPermission = none' in out


def test_legacy_and_new_representation_coexist() -> None:
    """Mixing legacy hidden_* with new add_table_permission is allowed;
    each renders in its own style."""
    role = TMDLRole(name="Mixed")
    role.add_table_permission(table_name="Sales", metadata_permission="none")
    role.hidden_tables.append("Customers")
    out = role.to_tmdl()
    assert "  tablePermission Sales" in out
    assert '"Customers" = metadataPermission = none' in out


def test_rls_filter_expression_renders_unchanged() -> None:
    """RLS-only roles still emit filterExpression as before."""
    role = TMDLRole(name="USOnly")
    role.add_table_permission(table_name="Sales", filter_expression='[Region] = "US"')
    out = role.to_tmdl()
    assert "  tablePermission Sales" in out
    assert "filterExpression" in out
    assert '[Region] = "US"' in out
    # No metadataPermission because this is a pure-RLS role.
    assert "metadataPermission" not in out


def test_model_permission_none_renders_unquoted() -> None:
    role = TMDLRole(name="HiddenRole", model_permission="none")
    out = role.to_tmdl()
    assert "  modelPermission = none" in out
    # Must NOT be quoted — Power BI rejects quoted enum values.
    assert 'modelPermission = "none"' not in out


# ---------------------------------------------------------------------------
# Parser round-trip
# ---------------------------------------------------------------------------


def test_parse_nested_tablepermission_blocks() -> None:
    text = """
        role CategoriesOLS {
            modelPermission = read
            tablePermission Customers
                metadataPermission = none
            tablePermission Sales
                columnPermission SaleId
                    metadataPermission = none
        }
    """
    role = parse_named_role(text, "CategoriesOLS")
    assert role.name == "CategoriesOLS"
    assert role.model_permission == "read"
    table_names = sorted(p.table_name for p in role.table_permissions)
    assert table_names == ["Customers", "Sales"]
    sales = next(p for p in role.table_permissions if p.table_name == "Sales")
    assert len(sales.columns) == 1
    assert sales.columns[0].column_name == "SaleId"
    assert sales.columns[0].metadata_permission == "none"
    customers = next(p for p in role.table_permissions if p.table_name == "Customers")
    assert customers.metadata_permission == "none"


def test_parse_legacy_tablepermissions_aggregator() -> None:
    """Older files use tablePermissions = [ "Tbl" = filterExpression: '' ]"""
    text = """
        role USOnly {
            tablePermissions = [
                "Sales" = filterExpression: '
                    [Region] = "US"
                '
            ]
        }
    """
    role = parse_named_role(text, "USOnly")
    sales = role.table_permissions[0]
    assert sales.table_name == "Sales"
    assert "[Region]" in (sales.filter_expression or "")
    assert sales.metadata_permission is None


def test_parse_mixed_new_and_legacy_in_same_role() -> None:
    text = """
        role Mixed {
            modelPermission = read
            tablePermission Sales
                columnPermission SaleId
                    metadataPermission = none
        }
    """
    role = parse_named_role(text, "Mixed")
    assert role.model_permission == "read"
    sales = role.table_permissions[0]
    assert sales.metadata_permission is None  # columnPermission doesn't set it
    assert len(sales.columns) == 1


def test_parse_filter_expression_with_apostrophes() -> None:
    """filterExpression may contain single quotes; parser must not truncate."""
    text = """
        role USOnly {
            tablePermission Sales
                filterExpression: '
                    [Region] = "US"
                '
        }
    """
    role = parse_named_role(text, "USOnly")
    sales = role.table_permissions[0]
    assert "[Region]" in (sales.filter_expression or "")


def test_parse_role_with_no_table_permissions() -> None:
    """A role with no tablePermission blocks is still valid."""
    text = """
        role Empty {
            modelPermission = read
        }
    """
    role = parse_named_role(text, "Empty")
    assert role.model_permission == "read"
    assert role.table_permissions == []


def test_parse_role_model_permission_none() -> None:
    text = """
        role NoAccess {
            modelPermission = none
        }
    """
    role = parse_named_role(text, "NoAccess")
    assert role.model_permission == "none"


def test_parse_combined_rls_and_ols_in_one_block_recovers() -> None:
    """If a file illegally mixes filterExpression + metadataPermission in
    one tablePermission block, the parser splits them into two entries
    rather than failing."""
    text = """
        role Bad {
            modelPermission = read
            tablePermission Sales
                filterExpression: '
                    [Region] = "US"
                '
                metadataPermission = none
        }
    """
    role = parse_named_role(text, "Bad")
    sales_perms = [p for p in role.table_permissions if p.table_name == "Sales"]
    # One entry has filter_expression, the other has metadata_permission.
    assert any(p.filter_expression is not None for p in sales_perms)
    assert any(p.metadata_permission is not None for p in sales_perms)


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def model_path(tmp_path: Path) -> Path:
    p = tmp_path / "model.tmdl"
    p.write_text("", encoding="utf-8")
    return p


def test_handler_emits_table_level_ols(tmp_path: Path, model_path: Path) -> None:
    result = add_ols_role_handler(
        role_name="CategoriesOLS",
        table_permissions=[{"table_name": "Customers", "metadata_permission": "none"}],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    assert result["table_permissions"][0]["table_name"] == "Customers"
    # The role is written under <model_path parent>/.roles/<name>.tmdl
    written = list(model_path.parent.glob(".roles/*.tmdl"))
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    assert "tablePermission Customers" in content
    assert "metadataPermission = none" in content


def test_handler_emits_column_level_ols(tmp_path: Path, model_path: Path) -> None:
    result = add_ols_role_handler(
        role_name="SalesPublic",
        column_permissions=[
            {
                "table_name": "Sales",
                "column_name": "SaleId",
                "metadata_permission": "none",
            }
        ],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    assert len(result["column_permissions"]) == 1
    written = list(model_path.parent.glob(".roles/*.tmdl"))
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    assert "tablePermission Sales" in content
    assert "columnPermission SaleId" in content


def test_handler_combines_table_and_column_rules(
    tmp_path: Path, model_path: Path
) -> None:
    add_ols_role_handler(
        role_name="Mixed",
        table_permissions=[{"table_name": "Customers", "metadata_permission": "none"}],
        column_permissions=[
            {
                "table_name": "Sales",
                "column_name": "SaleId",
                "metadata_permission": "none",
            }
        ],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    written = list(model_path.parent.glob(".roles/*.tmdl"))
    content = written[0].read_text(encoding="utf-8")
    assert "tablePermission Customers" in content
    assert "tablePermission Sales" in content
    assert "columnPermission SaleId" in content


def test_handler_legacy_hidden_tables_still_works(
    tmp_path: Path, model_path: Path
) -> None:
    """Legacy ``hidden_tables`` argument still works; the handler
    mirrors it into the canonical nested-tablePermission form (so
    Power BI Desktop gets the modern grammar) and the response keeps
    the original ``hidden_tables`` list populated for backward compat."""
    result = add_ols_role_handler(
        role_name="Legacy",
        hidden_tables=["Customers"],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    assert "Customers" in result["hidden_tables"]
    written = list(model_path.parent.glob(".roles/*.tmdl"))
    content = written[0].read_text(encoding="utf-8")
    # Now emitted as the canonical nested-tablePermission form.
    assert "tablePermission Customers" in content
    assert "metadataPermission = none" in content


def test_handler_legacy_hidden_columns_still_works(
    tmp_path: Path, model_path: Path
) -> None:
    result = add_ols_role_handler(
        role_name="Legacy",
        hidden_columns=[{"table_name": "Sales", "column_name": "SaleId"}],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    written = list(model_path.parent.glob(".roles/*.tmdl"))
    content = written[0].read_text(encoding="utf-8")
    assert "tablePermission Sales" in content
    assert "columnPermission SaleId" in content


def test_handler_requires_model_path() -> None:
    with pytest.raises(ValueError) as exc_info:
        add_ols_role_handler(role_name="X", context={})
    assert "model_path" in str(exc_info.value)


def test_handler_requires_at_least_one_rule(tmp_path: Path, model_path: Path) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_ols_role_handler(role_name="X", context={MODEL_PATH_KEY: str(model_path)})
    assert "at least one" in str(exc_info.value)


def test_handler_rejects_non_dict_table_permission_entry(
    tmp_path: Path, model_path: Path
) -> None:
    with pytest.raises(TMDLValidationError):
        add_ols_role_handler(
            role_name="X",
            table_permissions=["bad"],  # type: ignore[list-item]
            context={MODEL_PATH_KEY: str(model_path)},
        )


def test_handler_rejects_non_dict_column_permission_entry(
    tmp_path: Path, model_path: Path
) -> None:
    with pytest.raises(TMDLValidationError):
        add_ols_role_handler(
            role_name="X",
            column_permissions=["bad"],  # type: ignore[list-item]
            context={MODEL_PATH_KEY: str(model_path)},
        )


def test_handler_rejects_table_permission_missing_table_name(
    tmp_path: Path, model_path: Path
) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_ols_role_handler(
            role_name="X",
            table_permissions=[{"metadata_permission": "none"}],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "table_name" in str(exc_info.value)


def test_handler_rejects_column_permission_missing_keys(
    tmp_path: Path, model_path: Path
) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_ols_role_handler(
            role_name="X",
            column_permissions=[{"table_name": "Sales"}],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "column_name" in str(exc_info.value)


def test_handler_rejects_invalid_metadata_permission(
    tmp_path: Path, model_path: Path
) -> None:
    with pytest.raises(TMDLValidationError) as exc_info:
        add_ols_role_handler(
            role_name="X",
            table_permissions=[
                {"table_name": "Sales", "metadata_permission": "hidden"}
            ],
            context={MODEL_PATH_KEY: str(model_path)},
        )
    assert "'none' or 'read'" in str(exc_info.value)


def test_handler_accepts_camelcase_keys(tmp_path: Path, model_path: Path) -> None:
    """Camelcase aliases (tableName, columnName, metadataPermission) work too."""
    result = add_ols_role_handler(
        role_name="Camel",
        column_permissions=[
            {
                "tableName": "Sales",
                "columnName": "SaleId",
                "metadataPermission": "none",
            }
        ],
        context={MODEL_PATH_KEY: str(model_path)},
    )
    assert result["status"] == "success"
    assert result["column_permissions"][0]["table_name"] == "Sales"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_named_role(text: str, name: str) -> TMDLRole:
    """Parse a single role block by name from arbitrary TMDL text."""
    model = parse_tmdl_text(text)
    role = model.roles.get(name)
    if role is None:
        # The text might not be valid full-model TMDL; rebuild manually
        # via the role parser.
        # Extract the role body via simple regex (the test fixture
        # is well-formed so a tight regex is enough).
        import re

        from nl2pbip.tmdl_engine import _parse_role

        match = re.search(
            rf"role\s+(?:\"{re.escape(name)}\"|{re.escape(name)})\s*\{{",
            text,
        )
        assert match, f"role {name} not found in text"
        body_start = match.end() - 1
        # Find matching close brace.
        body, _ = _extract_balanced_for_test(text, body_start)
        return _parse_role(name, body)
    return role


def _extract_balanced_for_test(text: str, open_index: int) -> Any:
    """Return (inner_text, close_index) for the braces starting at open_index."""
    from nl2pbip.tmdl_engine import _extract_balanced_with

    return _extract_balanced_with(text, open_index, "{", "}")
