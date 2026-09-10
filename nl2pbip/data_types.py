"""Power BI TMDL data-type handling.

Power BI Desktop's TMDL parser is fairly permissive about which
``dataType`` spellings it accepts (e.g. ``int64``, ``wholeNumber``,
``string``, ``text`` are all valid) but the way the type is rendered in
measures, visuals, and DAX expressions depends on the canonical form.
For example:

* A ``decimal`` column without a ``formatString`` displays as raw digits;
  ``$#,0.00`` makes the model interpret it as currency. The choice
  belongs to the column author, not the model parser.
* An LLM that produces ``int64`` (SQL/Databricks convention) is
  accepted but the column will format as a plain integer rather than
  Power BI's native ``wholeNumber`` variant. Mixing both flavours in
  one model makes relationships fragile.
* An unknown type silently coerces to ``string`` — that hides bugs
  where an LLM emitted ``float`` or ``numeric`` for what was meant to
  be a numeric column, then measures silently treat the values as
  text.

This module centralises three concerns:

1. **Canonical TMDL types** — the small set Power BI Desktop actually
   understands natively (the Power BI docs spelling, not the SQL
   flavour). Anything else is either rejected or coerced via an
   alias mapping.
2. **Alias resolution** — common SQL/JSON/LLM-friendly spellings
   (``int``, ``bigint``, ``float``, ``varchar``) are mapped to the
   canonical TMDL form before the column hits disk. The original
   alias is preserved on the column object so error messages can tell
   the user what was rewritten.
3. **Default format strings** — for numeric, monetary, and temporal
   types, suggest a sensible ``formatString`` when the author didn't
   supply one. The suggestion is offered via
   :func:`suggest_default_format` and applied by :class:`TMDLColumn`'s
   constructor unless the caller passes ``format_string``.

Public API:
* :func:`normalize_data_type` — alias → canonical mapping, with a
  decision about whether unknown spellings should raise or fall back.
* :func:`validate_data_type` — strict check that a string is a known
  alias or canonical TMDL spelling.
* :func:`suggest_default_format` — return a default ``formatString``
  for a canonical type, or ``None`` if the type doesn't need one.
* :data:`CANONICAL_DATA_TYPES` — the canonical set.
* :data:`DATA_TYPE_ALIASES` — alias → canonical mapping.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Canonical TMDL data types — the Power BI Desktop-native spellings.
# Anything outside this set is either rejected or aliased.
# ---------------------------------------------------------------------------

CANONICAL_DATA_TYPES = frozenset(
    {
        "string",
        "int64",
        "double",
        "decimal",
        "boolean",
        "dateTime",
        "date",
        "time",
        "binary",
    }
)

# ---------------------------------------------------------------------------
# Aliases — common SQL/JSON/LLM-friendly spellings → canonical TMDL form.
#
# Notes:
#   - ``wholeNumber`` / ``decimalNumber`` / ``currency`` /
#     ``percentage`` / ``text`` / ``trueFalse`` / ``dateTime64`` /
#     ``dateTimeLocal`` are TMDL-native variants used by Power BI
#     Desktop's "Format" dropdown. They work but the engine writes
#     the cleaner Power Query-style names below when called via the
#     programmatic API. Callers who explicitly want a TMDL-native
#     format style can still pass the variant directly (it lives in
#     ``CANONICAL_DATA_TYPES`` extension set below).
#   - ``int32`` is intentionally NOT aliased — Power BI Desktop
#     silently coerces it to ``int64`` so accepting it just hides
#     intent. Reject early instead.
# ---------------------------------------------------------------------------

# TMDL-native variants Power BI Desktop's "Format" dropdown emits.
TMDL_FORMAT_VARIANTS = frozenset(
    {
        "wholeNumber",
        "decimalNumber",
        "currency",
        "percentage",
        "text",
        "trueFalse",
        "dateTime64",
        "dateTimeLocal",
    }
)

# The full set we accept on input.
ACCEPTED_DATA_TYPES = CANONICAL_DATA_TYPES | TMDL_FORMAT_VARIANTS

DATA_TYPE_ALIASES: dict[str, str] = {
    # SQL / T-SQL / Postgres / Snowflake / BigQuery / Databricks
    "bigint": "int64",
    "int": "int64",
    "integer": "int64",
    "smallint": "int64",
    "tinyint": "int64",
    "float": "double",
    "float64": "double",
    "double precision": "double",
    "real": "double",
    "numeric": "decimal",
    "money": "decimal",
    "smallmoney": "decimal",
    "datetime": "dateTime",
    "timestamp": "dateTime",
    "timestamptz": "dateTime",
    "smalldatetime": "dateTime",
    "datetime2": "dateTime",
    "char": "string",
    "varchar": "string",
    "nvarchar": "string",
    "text": "string",
    "nchar": "string",
    "ntext": "string",
    "clob": "string",
    "blob": "binary",
    "varbinary": "binary",
    "bytea": "binary",
    "bool": "boolean",
}


# ---------------------------------------------------------------------------
# Default format strings.
#
# Power BI defaults to General formatting, which displays decimals
# without thousand separators and dates as ISO 8601. The defaults
# below match what a data-modeller typically wants when they tag a
# column as a particular type.
# ---------------------------------------------------------------------------

DEFAULT_FORMAT_STRINGS: dict[str, Optional[str]] = {
    "decimal": "#,0.00",
    "double": "#,0.00",
    "int64": "#,0",
    "wholeNumber": "#,0",
    "decimalNumber": "#,0.00",
    "currency": "$#,0.00;-$#,0.00",
    "percentage": "0.00%",
    "dateTime": "yyyy-MM-dd HH:mm:ss",
    "dateTime64": "yyyy-MM-dd HH:mm:ss",
    "dateTimeLocal": "yyyy-MM-dd HH:mm:ss",
    "date": "yyyy-MM-dd",
    "time": "HH:mm:ss",
    "boolean": "TRUE/FALSE",
    "trueFalse": "TRUE/FALSE",
    "string": None,
    "text": None,
    "binary": None,
}


class DataTypeError(ValueError):
    """Raised when a column declares an unsupported dataType.

    Inherits ``ValueError`` so existing ``except ValueError`` blocks
    still catch it, but callers can discriminate via ``except
    DataTypeError`` when they need to surface a richer error to the
    LLM retry loop.
    """


def validate_data_type(data_type: str) -> None:
    """Raise :class:`DataTypeError` if ``data_type`` is not a known spelling.

    Accepts both canonical TMDL forms (e.g. ``int64``) and the
    variant spellings Power BI Desktop's "Format" dropdown emits
    (e.g. ``wholeNumber``).
    """
    if not isinstance(data_type, str):
        raise DataTypeError(
            f"dataType must be a string, got {type(data_type).__name__}."
        )
    if data_type in ACCEPTED_DATA_TYPES:
        return
    if data_type.lower() in {a.lower() for a in ACCEPTED_DATA_TYPES}:
        # Caller used the wrong case — case-insensitive match. We don't
        # auto-rewrite to keep canonicalisation explicit; just point
        # them at the right spelling.
        expected = next(
            a for a in ACCEPTED_DATA_TYPES if a.lower() == data_type.lower()
        )
        raise DataTypeError(
            f"dataType {data_type!r} is the wrong case; use {expected!r}."
        )
    if data_type.lower() in {a.lower() for a in DATA_TYPE_ALIASES}:
        canonical = _resolve_case_insensitive(data_type)
        raise DataTypeError(
            f"dataType {data_type!r} is an alias for {canonical!r}; "
            f"use the canonical spelling."
        )
    raise DataTypeError(
        f"Unknown dataType {data_type!r}. "
        f"Accepted canonical types: {sorted(ACCEPTED_DATA_TYPES)}. "
        f"Accepted aliases: {sorted(DATA_TYPE_ALIASES)}."
    )


def normalize_data_type(
    data_type: str,
    *,
    on_unknown: str = "raise",
) -> str:
    """Return the canonical TMDL spelling for ``data_type``.

    Parameters
    ----------
    data_type:
        The input spelling — either a canonical form, a variant
        (``wholeNumber``, ``text`` …), or an alias (``int``, ``float`` …).
    on_unknown:
        * ``"raise"`` (default) — call :func:`validate_data_type` and
          raise :class:`DataTypeError` on unrecognised spellings.
        * ``"fallback"`` — return ``"string"`` for any unknown input.
          Useful for parse paths where we genuinely want to be lenient
          (the TMDL parser, the linter) but NOT for the writer
          handler.
    """
    if not isinstance(data_type, str):
        if on_unknown == "fallback":
            return "string"
        raise DataTypeError(
            f"dataType must be a string, got {type(data_type).__name__}."
        )
    if data_type in ACCEPTED_DATA_TYPES:
        return data_type
    # Case-insensitive canonical match — use the canonical case.
    canonical_case = _resolve_case_insensitive(data_type)
    if canonical_case is not None:
        return canonical_case
    # Alias mapping.
    alias = DATA_TYPE_ALIASES.get(data_type.lower())
    if alias is not None:
        return alias
    if on_unknown == "fallback":
        return "string"
    # Build the rich error message from the validator.
    validate_data_type(data_type)
    # Defensive: validate_data_type should have raised already.
    raise DataTypeError(f"Unknown dataType {data_type!r}.")  # pragma: no cover


def suggest_default_format(data_type: str) -> Optional[str]:
    """Return a sensible default ``formatString`` for ``data_type``.

    Returns ``None`` when the type does not need a format string (e.g.
    ``string``, ``binary``). The format is only a suggestion — caller
    can override by passing ``format_string=`` explicitly.
    """
    canonical = normalize_data_type(data_type, on_unknown="fallback")
    return DEFAULT_FORMAT_STRINGS.get(canonical)


def _resolve_case_insensitive(data_type: str) -> Optional[str]:
    lower = data_type.lower()
    for candidate in ACCEPTED_DATA_TYPES:
        if candidate.lower() == lower:
            return candidate
    for alias, canonical in DATA_TYPE_ALIASES.items():
        if alias.lower() == lower:
            return canonical
    return None


__all__ = [
    "ACCEPTED_DATA_TYPES",
    "CANONICAL_DATA_TYPES",
    "DATA_TYPE_ALIASES",
    "DEFAULT_FORMAT_STRINGS",
    "DataTypeError",
    "normalize_data_type",
    "suggest_default_format",
    "validate_data_type",
]
