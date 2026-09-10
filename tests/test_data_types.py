"""Unit tests for the data-type handling layer.

Covers:
* Canonical TMDL spellings pass through ``normalize_data_type``.
* SQL / JSON aliases map to their canonical TMDL form.
* Aliases are case-insensitive.
* Unknown types raise :class:`DataTypeError` by default.
* ``on_unknown="fallback"`` returns ``"string"`` instead of raising.
* ``suggest_default_format`` returns sensible defaults for numeric,
  monetary, and temporal types and ``None`` for string/binary.
* ``TMDLColumn.__post_init__`` auto-applies format strings and
  preserves the caller's original spelling in
  ``original_data_type``.
* ``TMDLColumn`` rejects non-string and unknown data types at
  construction time.
* ``_validate_columns`` rewrites aliases on the input dict and
  raises :class:`TMDLValidationError` for genuinely unknown types.
* Round-trip through the parser: variant spellings written to disk
  by a third party are normalised back to the canonical form when
  parsed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.data_types import (
    ACCEPTED_DATA_TYPES,
    DATA_TYPE_ALIASES,
    DEFAULT_FORMAT_STRINGS,
    DataTypeError,
    normalize_data_type,
    suggest_default_format,
    validate_data_type,
)
from nl2pbip.tmdl_engine import (
    TMDLColumn,
    TMDLModel,
    TMDLTable,
    _validate_columns,
    parse_tmdl_text,
)
from nl2pbip.tmdl_linter import TMDLValidationError

# ---------------------------------------------------------------------------
# normalize_data_type
# ---------------------------------------------------------------------------


class TestNormalizeDataType:
    @pytest.mark.parametrize(
        "canonical",
        [
            "string",
            "int64",
            "double",
            "decimal",
            "boolean",
            "dateTime",
            "date",
            "time",
            "binary",
        ],
    )
    def test_canonical_spellings_pass_through(self, canonical: str) -> None:
        assert normalize_data_type(canonical) == canonical

    @pytest.mark.parametrize(
        "variant",
        [
            "wholeNumber",
            "decimalNumber",
            "currency",
            "percentage",
            "text",
            "trueFalse",
            "dateTime64",
            "dateTimeLocal",
        ],
    )
    def test_tmdl_variants_pass_through(self, variant: str) -> None:
        # Variants are accepted unchanged — callers who explicitly
        # use the Power BI "Format" dropdown spelling should get
        # exactly what they asked for.
        assert normalize_data_type(variant) == variant

    @pytest.mark.parametrize(
        "alias,canonical",
        [
            ("int", "int64"),
            ("bigint", "int64"),
            ("integer", "int64"),
            ("smallint", "int64"),
            ("tinyint", "int64"),
            ("float", "double"),
            ("float64", "double"),
            ("double precision", "double"),
            ("real", "double"),
            ("numeric", "decimal"),
            ("money", "decimal"),
            ("smallmoney", "decimal"),
            ("datetime", "dateTime"),
            ("timestamp", "dateTime"),
            ("timestamptz", "dateTime"),
            ("datetime2", "dateTime"),
            ("smalldatetime", "dateTime"),
            ("char", "string"),
            ("varchar", "string"),
            ("nvarchar", "string"),
            ("nchar", "string"),
            ("ntext", "string"),
            ("clob", "string"),
            ("blob", "binary"),
            ("varbinary", "binary"),
            ("bytea", "binary"),
            ("bool", "boolean"),
        ],
    )
    def test_alias_maps_to_canonical(self, alias: str, canonical: str) -> None:
        assert normalize_data_type(alias) == canonical

    def test_aliases_are_case_insensitive(self) -> None:
        assert normalize_data_type("BIGINT") == "int64"
        assert normalize_data_type("Float") == "double"
        assert normalize_data_type("VarChar") == "string"
        assert normalize_data_type("TIMESTAMP") == "dateTime"

    def test_unknown_type_raises_by_default(self) -> None:
        with pytest.raises(DataTypeError) as exc_info:
            normalize_data_type("floob")
        assert "floob" in str(exc_info.value)

    def test_unknown_type_returns_string_in_fallback_mode(self) -> None:
        assert normalize_data_type("floob", on_unknown="fallback") == "string"
        assert normalize_data_type("", on_unknown="fallback") == "string"

    def test_non_string_raises(self) -> None:
        with pytest.raises(DataTypeError):
            normalize_data_type(42)  # type: ignore[arg-type]
        # In fallback mode, non-strings also coerce to string.
        assert normalize_data_type(42, on_unknown="fallback") == "string"  # type: ignore[arg-type]

    def test_error_message_lists_alias(self) -> None:
        """An LLM that emits an alias should learn what canonical spelling to use.

        ``validate_data_type`` is the strict preflight check that
        rejects aliases (so the LLM retry loop learns to use the
        canonical spelling). ``normalize_data_type`` accepts aliases
        and rewrites them.
        """
        with pytest.raises(DataTypeError) as exc_info:
            validate_data_type("bigint")
        assert "bigint" in str(exc_info.value)
        assert "int64" in str(exc_info.value)


# ---------------------------------------------------------------------------
# validate_data_type
# ---------------------------------------------------------------------------


class TestValidateDataType:
    def test_accepts_canonical_and_variants(self) -> None:
        validate_data_type("int64")  # canonical
        validate_data_type("wholeNumber")  # variant
        validate_data_type("currency")  # variant

    def test_rejects_alias_by_name(self) -> None:
        # Aliases are accepted by normalize_data_type but NOT by
        # validate_data_type (which is the strict preflight check).
        with pytest.raises(DataTypeError) as exc_info:
            validate_data_type("bigint")
        assert "bigint" in str(exc_info.value)
        assert "int64" in str(exc_info.value)

    def test_rejects_unknown(self) -> None:
        with pytest.raises(DataTypeError) as exc_info:
            validate_data_type("floob")
        assert "floob" in str(exc_info.value)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(DataTypeError):
            validate_data_type(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# suggest_default_format
# ---------------------------------------------------------------------------


class TestSuggestDefaultFormat:
    @pytest.mark.parametrize(
        "data_type,expected",
        [
            ("decimal", "#,0.00"),
            ("double", "#,0.00"),
            ("int64", "#,0"),
            ("wholeNumber", "#,0"),
            ("decimalNumber", "#,0.00"),
            ("currency", "$#,0.00;-$#,0.00"),
            ("percentage", "0.00%"),
            ("dateTime", "yyyy-MM-dd HH:mm:ss"),
            ("date", "yyyy-MM-dd"),
            ("time", "HH:mm:ss"),
            ("boolean", "TRUE/FALSE"),
            ("trueFalse", "TRUE/FALSE"),
        ],
    )
    def test_suggests_default_format(self, data_type: str, expected: str) -> None:
        assert suggest_default_format(data_type) == expected

    @pytest.mark.parametrize("data_type", ["string", "text", "binary"])
    def test_string_and_binary_have_no_default_format(self, data_type: str) -> None:
        assert suggest_default_format(data_type) is None

    def test_aliases_get_format_via_normalization(self) -> None:
        # Aliases route through normalize_data_type so they get the
        # same format suggestions as their canonical forms.
        assert suggest_default_format("bigint") == "#,0"
        assert suggest_default_format("varchar") is None
        assert suggest_default_format("bool") == "TRUE/FALSE"

    def test_unknown_type_returns_none(self) -> None:
        # Unknown types fall back to string (no format) rather than
        # raising — that's the "I don't know, suggest nothing" path.
        assert suggest_default_format("floob") is None


# ---------------------------------------------------------------------------
# TMDLColumn
# ---------------------------------------------------------------------------


class TestTMDLColumnAutoNormalisation:
    def test_canonical_passes_through(self) -> None:
        c = TMDLColumn(name="x", data_type="decimal")
        assert c.data_type == "decimal"
        assert c.original_data_type == "decimal"

    def test_alias_is_normalised(self) -> None:
        c = TMDLColumn(name="revenue", data_type="money")
        assert c.data_type == "decimal"
        assert c.original_data_type == "money"

    def test_default_format_is_applied(self) -> None:
        c = TMDLColumn(name="amount", data_type="decimal")
        assert c.format_string == "#,0.00"

    def test_default_format_via_alias(self) -> None:
        c = TMDLColumn(name="count", data_type="bigint")
        assert c.data_type == "int64"
        assert c.format_string == "#,0"

    def test_caller_format_wins_over_default(self) -> None:
        c = TMDLColumn(name="x", data_type="decimal", format_string="0.0")
        assert c.format_string == "0.0"

    def test_string_type_has_no_default_format(self) -> None:
        c = TMDLColumn(name="name", data_type="string")
        assert c.format_string is None

    def test_unknown_type_raises_tmdl_validation_error(self) -> None:
        with pytest.raises(TMDLValidationError) as exc_info:
            TMDLColumn(name="x", data_type="floob")
        assert "floob" in str(exc_info.value)
        assert "x" in str(exc_info.value)

    def test_non_string_type_raises(self) -> None:
        with pytest.raises(TMDLValidationError) as exc_info:
            TMDLColumn(name="x", data_type=42)  # type: ignore[arg-type]
        assert "must be a string" in str(exc_info.value)

    def test_to_tmdl_emits_format_string(self) -> None:
        c = TMDLColumn(name="amount", data_type="decimal")
        rendered = c.to_tmdl()
        assert "dataType = decimal" in rendered
        assert 'formatString = "#,0.00"' in rendered

    def test_to_tmdl_skips_format_when_none(self) -> None:
        c = TMDLColumn(name="name", data_type="string")
        rendered = c.to_tmdl()
        assert "formatString" not in rendered


# ---------------------------------------------------------------------------
# _validate_columns (the handler preflight check)
# ---------------------------------------------------------------------------


class TestValidateColumns:
    def test_accepts_canonical_and_aliases(self) -> None:
        # Aliases should be silently normalised, not rejected — the
        # LLM retry loop shouldn't be triggered by a benign rewrite.
        cols: List[Dict[str, Any]] = [
            {"name": "Revenue", "data_type": "bigint"},
            {"name": "Region", "data_type": "varchar"},
            {"name": "Id", "data_type": "int64"},
        ]
        _validate_columns("Sales", cols)
        assert cols[0]["data_type"] == "int64"
        assert cols[1]["data_type"] == "string"
        assert cols[2]["data_type"] == "int64"

    def test_accepts_camel_case_dataType(self) -> None:
        cols: List[Dict[str, Any]] = [
            {"name": "X", "dataType": "wholeNumber"},
        ]
        _validate_columns("T", cols)
        assert cols[0]["data_type"] == "wholeNumber"

    def test_rejects_unknown_type(self) -> None:
        cols: List[Dict[str, Any]] = [
            {"name": "X", "data_type": "floob"},
        ]
        with pytest.raises(TMDLValidationError) as exc_info:
            _validate_columns("T", cols)
        assert "floob" in str(exc_info.value)
        assert "X" in str(exc_info.value)
        assert "T" in str(exc_info.value)

    def test_rejects_non_dict_column(self) -> None:
        with pytest.raises(TMDLValidationError):
            _validate_columns("T", ["not a dict"])  # type: ignore[list-item]

    def test_rejects_column_without_name(self) -> None:
        with pytest.raises(TMDLValidationError):
            _validate_columns("T", [{"data_type": "int64"}])

    def test_defaults_to_string_when_type_missing(self) -> None:
        cols: List[Dict[str, Any]] = [{"name": "X"}]
        _validate_columns("T", cols)
        assert cols[0]["data_type"] == "string"


# ---------------------------------------------------------------------------
# Parser round-trip with variant spellings
# ---------------------------------------------------------------------------


class TestParserNormalisation:
    def test_variant_spellings_normalised_on_parse(self) -> None:
        # A third-party TMDL file written with `wholeNumber` and `text`
        # should parse cleanly and round-trip as the same variant (we
        # don't rewrite variant spellings — only aliases).
        text = (
            'table "T" {\n'
            "  columns = [\n"
            '    column "id" {\n'
            "      dataType = wholeNumber\n"
            "    },\n"
            '    column "name" {\n'
            "      dataType = text\n"
            "    },\n"
            '    column "amount" {\n'
            "      dataType = decimal\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )
        model = parse_tmdl_text(text)
        t = model.get_table("T")
        assert t is not None
        assert t.columns["id"].data_type == "wholeNumber"
        assert t.columns["name"].data_type == "text"
        # decimal still gets the auto-suggested format because the
        # parsed column had no formatString on disk.
        assert t.columns["amount"].format_string == "#,0.00"
        assert t.columns["id"].format_string == "#,0"

    def test_existing_format_string_survives_parse(self) -> None:
        text = (
            'table "T" {\n'
            "  columns = [\n"
            '    column "amount" {\n'
            "      dataType = decimal\n"
            '      formatString = "0.000"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )
        model = parse_tmdl_text(text)
        table = model.get_table("T")
        assert table is not None
        col = table.columns["amount"]
        assert col.format_string == "0.000"


# ---------------------------------------------------------------------------
# End-to-end: create_table handler accepts aliases
# ---------------------------------------------------------------------------


class TestCreateTableHandlerAcceptsAliases:
    def test_alias_creates_column_with_canonical_type(self, tmp_path: Path) -> None:
        from nl2pbip.tmdl_engine import create_table_handler

        model_path = tmp_path / "model.tmdl"
        result = create_table_handler(
            table_name="Sales",
            columns=[
                {"name": "Id", "data_type": "bigint"},
                {"name": "Amount", "data_type": "money"},
                {"name": "CreatedAt", "data_type": "timestamp"},
                {"name": "Region", "data_type": "varchar"},
            ],
            context={"model_path": str(model_path)},
        )
        assert result["status"] == "success"
        # The on-disk file uses canonical TMDL spellings.
        body = model_path.read_text()
        assert "dataType = int64" in body
        assert "dataType = decimal" in body
        assert "dataType = dateTime" in body
        assert "dataType = string" in body
        # Aliases have been normalised + auto-formats applied.
        assert 'formatString = "#,0"' in body  # int64 default
        assert 'formatString = "#,0.00"' in body  # decimal default
        assert 'formatString = "yyyy-MM-dd HH:mm:ss"' in body  # dateTime default

    def test_unknown_type_raises_before_writing(self, tmp_path: Path) -> None:
        from nl2pbip.tmdl_engine import create_table_handler

        model_path = tmp_path / "model.tmdl"
        with pytest.raises(TMDLValidationError) as exc_info:
            create_table_handler(
                table_name="T",
                columns=[{"name": "X", "data_type": "floob"}],
                context={"model_path": str(model_path)},
            )
        assert "floob" in str(exc_info.value)
        # Crucially: no file is written when validation fails.
        assert not model_path.exists()
