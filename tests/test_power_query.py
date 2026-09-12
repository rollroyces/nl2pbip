"""Tests for the Power Query M expression builders + partition handler.

Three layers of test:

1. ``m_builder`` unit tests — quoting, validation, builder outputs
   for each template, transformation builders.
2. ``add_power_query_partition`` handler tests — template dispatch,
   raw M mode, validation, round-trip through TMDL.
3. Parser regression — multi-line ``expression = "..."`` values
   round-trip cleanly through ``load_model``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from nl2pbip.m_builder import (
    TEMPLATE_BUILDERS,
    build_append_query,
    build_csv_source,
    build_from_template,
    build_group_aggregate,
    build_json_source,
    build_merge_query,
    build_odata_source,
    build_promoted_table,
    build_sharepoint_source,
    build_sql_source,
    build_web_source,
    quote_identifier,
    quote_string,
    validate_m_expression,
)
from nl2pbip.tmdl_engine import (
    MODEL_PATH_KEY,
    TMDLValidationError,
    add_power_query_partition_handler,
    create_table_handler,
    load_model,
)

# ---------------------------------------------------------------------------
# quoting
# ---------------------------------------------------------------------------


class TestQuoting:
    def test_quote_string_plain(self) -> None:
        assert quote_string("hello") == '"hello"'

    def test_quote_string_with_embedded_quote(self) -> None:
        # M escapes embedded " by doubling it.
        assert quote_string('say "hi"') == '"say ""hi"""'

    def test_quote_string_with_backslash(self) -> None:
        # Backslash is NOT an escape character in M.
        assert quote_string(r"C:\data\sales.csv") == '"C:\\data\\sales.csv"'

    def test_quote_string_empty(self) -> None:
        assert quote_string("") == '""'

    def test_quote_identifier_plain(self) -> None:
        assert quote_identifier("Date") == '#"Date"'

    def test_quote_identifier_with_spaces(self) -> None:
        assert quote_identifier("Has Spaces") == '#"Has Spaces"'

    def test_quote_identifier_with_quote(self) -> None:
        assert quote_identifier('Has "Quote"') == '#"Has ""Quote"""'


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_minimal(self) -> None:
        validate_m_expression("let x = 1 in x")

    def test_valid_no_let(self) -> None:
        validate_m_expression("Source = 1")

    def test_valid_multiline(self) -> None:
        expr = "let\n    Source = 1\nin\n    Source"
        validate_m_expression(expr)

    def test_invalid_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_m_expression("")

    def test_invalid_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_m_expression("   \n  ")

    def test_invalid_unbalanced_let_no_in(self) -> None:
        with pytest.raises(ValueError, match="no matching 'in'"):
            validate_m_expression("let x = 1")

    def test_invalid_unbalanced_let(self) -> None:
        with pytest.raises(ValueError, match="unbalanced"):
            validate_m_expression("let x = 1\nlet y = 2\nin z")

    def test_invalid_embedded_triple_quote(self) -> None:
        with pytest.raises(ValueError, match="triple-quote"):
            validate_m_expression("let x = '''abc''' in x")

    def test_invalid_nul_byte(self) -> None:
        with pytest.raises(ValueError, match="NUL"):
            validate_m_expression("let x = \x00 in x")

    def test_invalid_too_long(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            validate_m_expression("let x = " + ("a" * 1_000_010) + " in x")

    def test_invalid_not_a_string(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            validate_m_expression(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# source builders
# ---------------------------------------------------------------------------


class TestCsvBuilder:
    def test_basic(self) -> None:
        result = build_csv_source("/data/sales.csv")
        assert "/data/sales.csv" in result
        assert "Csv.Document" in result
        assert "File.Contents" in result

    def test_quoted_path_with_spaces(self) -> None:
        result = build_csv_source("/Users/My Name/data.csv")
        assert '"/Users/My Name/data.csv"' in result

    def test_custom_delimiter(self) -> None:
        result = build_csv_source("/data/x.tsv", delimiter="\t")
        # M does NOT escape backslashes, so the delimiter is emitted
        # as a literal tab inside the M string literal.
        assert 'Delimiter="\t"' in result

    def test_empty_path_raises(self) -> None:
        with pytest.raises(ValueError, match="path"):
            build_csv_source("")


class TestSqlBuilder:
    def test_basic(self) -> None:
        result = build_sql_source("localhost", "Sales", "SELECT * FROM dbo.Sales")
        assert 'Sql.Database("localhost", "Sales"' in result
        assert '"SELECT * FROM dbo.Sales"' in result

    def test_with_privacy(self) -> None:
        result = build_sql_source(
            "localhost", "Sales", "SELECT 1", privacy="Privacy.None"
        )
        assert "Privacy.None" in result

    def test_empty_server_raises(self) -> None:
        with pytest.raises(ValueError, match="server"):
            build_sql_source("", "Sales", "SELECT 1")

    def test_empty_database_raises(self) -> None:
        with pytest.raises(ValueError, match="database"):
            build_sql_source("localhost", "", "SELECT 1")

    def test_empty_query_raises(self) -> None:
        with pytest.raises(ValueError, match="query"):
            build_sql_source("localhost", "Sales", "")


class TestJsonBuilder:
    def test_basic(self) -> None:
        result = build_json_source("/data/api.json")
        assert "/data/api.json" in result
        assert "Json.Document" in result


class TestSharePointBuilder:
    def test_basic(self) -> None:
        result = build_sharepoint_source(
            "https://contoso.sharepoint.com/sites/bi",
            "Shared Documents/sales.xlsx",
        )
        assert "SharePoint.Files" in result
        assert "https://contoso.sharepoint.com/sites/bi" in result


class TestODataBuilder:
    def test_basic(self) -> None:
        result = build_odata_source("https://services.odata.org/V4/Northwind")
        assert "OData.Feed" in result


class TestWebBuilder:
    def test_basic(self) -> None:
        result = build_web_source("https://api.example.com/v1/sales")
        assert "Web.Contents" in result


# ---------------------------------------------------------------------------
# transformation builders
# ---------------------------------------------------------------------------


class TestPromotedTable:
    def test_basic_promote(self) -> None:
        staging = build_csv_source("/data/sales.csv")
        result = build_promoted_table(staging, "Sales")
        assert "Table.PromoteHeaders" in result
        assert '#"Sales"' in result

    def test_promote_with_column_types(self) -> None:
        staging = build_csv_source("/data/sales.csv")
        result = build_promoted_table(
            staging,
            "Sales",
            column_types=[
                {"name": "Id", "type": "text"},
                {"name": "Amount", "type": "number"},
            ],
        )
        assert "Table.TransformColumnTypes" in result
        assert '{"Id", text}' in result
        assert '{"Amount", number}' in result

    def test_column_types_without_name_raises(self) -> None:
        staging = build_csv_source("/data/sales.csv")
        with pytest.raises(ValueError, match="name"):
            build_promoted_table(
                staging,
                "Sales",
                column_types=[{"type": "text"}],  # missing name
            )

    def test_column_types_without_type_raises(self) -> None:
        staging = build_csv_source("/data/sales.csv")
        with pytest.raises(ValueError, match="type"):
            build_promoted_table(
                staging,
                "Sales",
                column_types=[{"name": "Id"}],  # missing type
            )


class TestMergeQuery:
    def test_single_key(self) -> None:
        result = build_merge_query(
            "LeftTable",
            "RightTable",
            [{"left": "Id", "right": "Id"}],
        )
        assert "Table.NestedJoin" in result
        assert "JoinKind.LeftOuter" in result

    def test_multiple_keys(self) -> None:
        result = build_merge_query(
            "LeftTable",
            "RightTable",
            [{"left": "A", "right": "X"}, {"left": "B", "right": "Y"}],
        )
        # Multiple keys are joined in a single set per side, not as
        # separate {key, key} blocks.
        assert '{#"A", #"B"}' in result
        assert '{#"X", #"Y"}' in result

    def test_invalid_kind(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            build_merge_query(
                "Left", "Right", [{"left": "Id", "right": "Id"}], kind="Bogus"
            )


class TestAppendQuery:
    def test_two_queries(self) -> None:
        result = build_append_query(["SourceA", "SourceB"])
        assert "Table.Combine" in result
        assert "Q0" in result
        assert "Q1" in result

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            build_append_query([])


class TestGroupAggregate:
    def test_basic(self) -> None:
        result = build_group_aggregate(
            "Source",
            ["Region"],
            [{"column": "Amount", "function": "Sum", "alias": "Total"}],
        )
        assert "Table.Group" in result
        assert '{#"Region"}' in result
        assert '"Total"' in result

    def test_invalid_function(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            build_group_aggregate(
                "Source",
                ["Region"],
                [{"column": "Amount", "function": "Bogus"}],
            )


# ---------------------------------------------------------------------------
# template dispatcher
# ---------------------------------------------------------------------------


class TestTemplateDispatch:
    def test_dispatch_csv(self) -> None:
        result = build_from_template("csv", {"path": "/data/x.csv"})
        assert "Csv.Document" in result

    def test_dispatch_sql(self) -> None:
        result = build_from_template(
            "sql", {"server": "s", "database": "d", "query": "SELECT 1"}
        )
        assert "Sql.Database" in result

    def test_dispatch_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            build_from_template("bogus", {})

    def test_all_templates_dispatchable(self) -> None:
        for name in TEMPLATE_BUILDERS:
            params: Dict[str, Any] = {"path": "/x"}
            if name == "sql":
                params = {"server": "s", "database": "d", "query": "SELECT 1"}
            elif name in ("odata", "web"):
                params = {"url": "https://example.com"}
            elif name == "sharepoint":
                params = {
                    "site_url": "https://x.sharepoint.com",
                    "file_path": "f.xlsx",
                }
            result = build_from_template(name, params)
            assert result.strip()


# ---------------------------------------------------------------------------
# add_power_query_partition handler
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_model_path(tmp_path: Path) -> Dict[str, Any]:
    """Set up a model file with one table for partition tests."""
    model_path = tmp_path / "model.tmdl"
    ctx: Dict[str, Any] = {MODEL_PATH_KEY: str(model_path)}
    create_table_handler(
        "Sales",
        [
            {"name": "SaleId", "data_type": "string"},
            {"name": "Region", "data_type": "string"},
            {"name": "Amount", "data_type": "decimal"},
        ],
        context=ctx,
    )
    return ctx


class TestHandlerTemplateMode:
    def test_csv_template(self, tmp_model_path: Dict[str, Any]) -> None:
        result = add_power_query_partition_handler(
            "Sales",
            template="csv",
            params={"path": "/data/sales.csv"},
            context=tmp_model_path,
        )
        assert result["status"] == "success"
        assert result["template"] == "csv"
        assert result["promote"] is False
        text = Path(tmp_model_path[MODEL_PATH_KEY]).read_text()
        assert "Csv.Document" in text

    def test_sql_template(self, tmp_model_path: Dict[str, Any]) -> None:
        result = add_power_query_partition_handler(
            "Sales",
            template="sql",
            params={
                "server": "localhost",
                "database": "Sales",
                "query": "SELECT * FROM dbo.Sales",
            },
            context=tmp_model_path,
        )
        assert result["status"] == "success"
        text = Path(tmp_model_path[MODEL_PATH_KEY]).read_text()
        assert "Sql.Database" in text

    def test_json_template(self, tmp_model_path: Dict[str, Any]) -> None:
        result = add_power_query_partition_handler(
            "Sales",
            template="json",
            params={"path": "/data/api.json"},
            context=tmp_model_path,
        )
        assert result["status"] == "success"
        text = Path(tmp_model_path[MODEL_PATH_KEY]).read_text()
        assert "Json.Document" in text

    def test_with_promote_flag(self, tmp_model_path: Dict[str, Any]) -> None:
        result = add_power_query_partition_handler(
            "Sales",
            template="csv",
            params={"path": "/data/sales.csv"},
            promote=True,
            column_types=[
                {"name": "SaleId", "type": "text"},
                {"name": "Amount", "type": "number"},
            ],
            context=tmp_model_path,
        )
        assert result["status"] == "success"
        assert result["promote"] is True
        assert result["column_types"] == 2
        text = Path(tmp_model_path[MODEL_PATH_KEY]).read_text()
        assert "Table.PromoteHeaders" in text
        assert "Table.TransformColumnTypes" in text


class TestHandlerRawM:
    def test_raw_m_expression(self, tmp_model_path: Dict[str, Any]) -> None:
        m = "let Source = 1 in Source"
        result = add_power_query_partition_handler(
            "Sales", m_expression=m, context=tmp_model_path
        )
        assert result["status"] == "success"
        text = Path(tmp_model_path[MODEL_PATH_KEY]).read_text()
        assert "let Source = 1 in Source" in text

    def test_raw_m_with_promote(self, tmp_model_path: Dict[str, Any]) -> None:
        m = "let Source = Csv.Document(...) in Source"
        result = add_power_query_partition_handler(
            "Sales",
            m_expression=m,
            promote=True,
            column_types=[{"name": "SaleId", "type": "text"}],
            context=tmp_model_path,
        )
        assert result["status"] == "success"
        text = Path(tmp_model_path[MODEL_PATH_KEY]).read_text()
        assert "Table.PromoteHeaders" in text

    def test_invalid_m_expression(self, tmp_model_path: Dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            add_power_query_partition_handler(
                "Sales", m_expression="let x = 1", context=tmp_model_path
            )


class TestHandlerValidation:
    def test_missing_context(self) -> None:
        with pytest.raises(ValueError, match="model_path"):
            add_power_query_partition_handler(
                "Sales", template="csv", params={"path": "/x"}
            )

    def test_empty_table_name(self, tmp_model_path: Dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="table_name"):
            add_power_query_partition_handler(
                "", template="csv", params={"path": "/x"}, context=tmp_model_path
            )

    def test_no_template_or_m(self, tmp_model_path: Dict[str, Any]) -> None:
        with pytest.raises(TMDLValidationError, match="template"):
            add_power_query_partition_handler("Sales", context=tmp_model_path)

    def test_both_template_and_m(self, tmp_model_path: Dict[str, Any]) -> None:
        with pytest.raises(TMDLValidationError, match="OR"):
            add_power_query_partition_handler(
                "Sales",
                template="csv",
                params={"path": "/x"},
                m_expression="let x = 1 in x",
                context=tmp_model_path,
            )

    def test_unknown_template(self, tmp_model_path: Dict[str, Any]) -> None:
        with pytest.raises(TMDLValidationError, match="unknown template"):
            add_power_query_partition_handler(
                "Sales",
                template="bogus",
                params={},
                context=tmp_model_path,
            )

    def test_invalid_mode(self, tmp_model_path: Dict[str, Any]) -> None:
        with pytest.raises(TMDLValidationError, match="mode"):
            add_power_query_partition_handler(
                "Sales",
                template="csv",
                params={"path": "/x"},
                mode="streaming",
                context=tmp_model_path,
            )

    def test_table_does_not_exist(self, tmp_model_path: Dict[str, Any]) -> None:
        with pytest.raises(TMDLValidationError, match="does not exist"):
            add_power_query_partition_handler(
                "NonExistent",
                template="csv",
                params={"path": "/x"},
                context=tmp_model_path,
            )

    def test_duplicate_partition_without_replace(
        self, tmp_model_path: Dict[str, Any]
    ) -> None:
        add_power_query_partition_handler(
            "Sales",
            template="csv",
            params={"path": "/data/sales.csv"},
            context=tmp_model_path,
        )
        with pytest.raises(TMDLValidationError, match="already has"):
            add_power_query_partition_handler(
                "Sales",
                template="csv",
                params={"path": "/data/sales2.csv"},
                context=tmp_model_path,
            )

    def test_duplicate_partition_with_replace(
        self, tmp_model_path: Dict[str, Any]
    ) -> None:
        add_power_query_partition_handler(
            "Sales",
            template="csv",
            params={"path": "/data/sales.csv"},
            context=tmp_model_path,
        )
        result = add_power_query_partition_handler(
            "Sales",
            template="csv",
            params={"path": "/data/sales2.csv"},
            replace=True,
            context=tmp_model_path,
        )
        assert result["status"] == "success"


class TestHandlerRoundTrip:
    def test_partition_round_trips_through_parser(
        self, tmp_model_path: Dict[str, Any]
    ) -> None:
        add_power_query_partition_handler(
            "Sales",
            template="csv",
            params={"path": "/data/sales.csv"},
            promote=True,
            column_types=[{"name": "SaleId", "type": "text"}],
            context=tmp_model_path,
        )
        # Parse the model back and verify the expression is recoverable.
        parsed = load_model(Path(tmp_model_path[MODEL_PATH_KEY]))
        tbl = parsed.get_table("Sales")
        assert tbl is not None
        assert len(tbl.partitions) == 1
        partition = tbl.partitions[0]
        assert partition["name"] == "Sales"
        assert partition["mode"] == "import"
        assert "source" in partition
        source = partition["source"]
        assert source["type"] == "m"
        # Expression should contain a normalised version of the M.
        assert "Csv.Document" in source["expression"]
        assert "/data/sales.csv" in source["expression"]
        assert "Table.PromoteHeaders" in source["expression"]

    def test_quoted_strings_in_path_round_trip(
        self, tmp_model_path: Dict[str, Any]
    ) -> None:
        # Path with quote characters — exercises the "" escape.
        add_power_query_partition_handler(
            "Sales",
            template="csv",
            params={"path": '/data/say"hi".csv'},
            context=tmp_model_path,
        )
        parsed = load_model(Path(tmp_model_path[MODEL_PATH_KEY]))
        tbl = parsed.get_table("Sales")
        assert tbl is not None
        expr = tbl.partitions[0]["source"]["expression"]
        # Round-trip: the writer applied one level of M-escape (so
        # the on-disk form had "" "" "" "" around the embedded ")
        # and the parser reduced it back to the single-escaped form
        # that the builder produced. The original raw path is not
        # recoverable without re-running the M parser — the right
        # invariant is that the parsed expression contains the M
        # string literal (with "" escape) and points at the right
        # file.
        assert 'File.Contents("/data/say""hi"".csv")' in expr


class TestHandlerExampleRun:
    def test_full_example_plan_includes_power_query_step(self) -> None:
        """The bundled example_run.py should now contain an
        ``add_power_query_partition`` step. Sanity-check via import."""
        from nl2pbip.example_run import build_sample_plan

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            plan = build_sample_plan(artifact_dir)
            tools = [step["tool"] for step in plan]
            assert "add_power_query_partition" in tools
