"""Power Query M expression builders.

The TMDL spec stores M queries inside a partition's ``source =
{ expressionSource: m, expression = "..." }`` block. This module
provides safe, deterministic builders for the most common Power
Query templates (CSV, SQL, JSON, SharePoint, OData, web, etc.) and
for the typical transformation patterns (promoted header, changed
type, merge, append, group-aggregate).

Why builders?
-------------
* Safe quoting. Power Query's M language uses double quotes for
  strings; embedded double quotes are escaped by doubling them. Naive
  f-string templating will produce silently broken queries the first
  time a path or column name contains a quote. ``quote_string()`` is
  the single place that knows the rule.
* Deterministic output. Two calls with the same arguments must
  produce byte-identical strings, otherwise the orchestrator's
  idempotency tests will flake.
* No eval. The builders return strings; nothing in this module
  executes M code. The handler in ``tmdl_engine.py`` only validates
  shape, never evaluates.

The builders here are intentionally lightweight; they cover the
common cases. For a fully custom query, pass a verbatim ``m_expression``
through ``add_power_query_partition``; the orchestrator will record it
inside the partition block without modification.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Low-level M quoting helpers
# ---------------------------------------------------------------------------


def quote_string(value: str) -> str:
    """Return ``value`` as a safe M double-quoted string literal.

    M escapes embedded double quotes by doubling them. A backslash is
    NOT an escape character in M strings, so we leave it alone.

    Note: this wraps ``value`` in ``"..."``. If you have M source
    code that *already* contains string literals (i.e. the M text
    uses ``"..."`` quoting for its inner strings), pass the whole
    M text through :func:`m_escape` instead of wrapping each
    string separately — that helper only escapes the existing
    quotes, without adding another layer.
    """
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def m_escape(value: str) -> str:
    """Escape a string for inclusion inside an M string literal.

    Power Query M escapes embedded ``"`` by doubling it. Unlike
    :func:`quote_string`, this helper does NOT add the wrapping
    ``"..."``; use it when the input is already a piece of M source
    code that you want to embed as-is in a TMDL ``expression = "..."``
    block.
    """
    return value.replace('"', '""')


def quote_identifier(value: str) -> str:
    """Quote an M identifier (``#"Name With Spaces"``).

    M identifiers can be quoted with ``#"..."``. Embedded ``"`` are
    doubled.
    """
    escaped = value.replace('"', '""')
    return f'#"{escaped}"'


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_m_expression(expression: str) -> None:
    """Raise ``ValueError`` if ``expression`` is not a plausible M query.

    Checks performed (cheap, deterministic; no parse):

    * Non-empty
    * Balanced ``let ... in`` (only if ``let`` appears at all)
    * No triple-quoted M-strings (those need a separate escape path
      in the partition writer, which we don't yet support)
    * No NUL bytes (Power Query will reject those at parse time)
    * Length within a sane ceiling (1 MB) — longer queries almost
      certainly indicate a logic bug.

    The point isn't to fully parse M. It's to catch the
    high-frequency mistake of an unbalanced ``let`` / ``in`` before
    the LLM's output reaches the .pbip file.
    """
    if not isinstance(expression, str):
        raise ValueError("M expression must be a string.")
    if not expression.strip():
        raise ValueError("M expression must be non-empty.")
    if "\x00" in expression:
        raise ValueError("M expression contains a NUL byte.")
    if len(expression) > 1_000_000:
        raise ValueError(
            f"M expression is too long ({len(expression):,} chars; " "max 1,000,000)."
        )
    # Balance check — only if 'let' is present.
    if "let" in expression and "in" in expression:
        # Use word boundaries; ``let`` / ``in`` keywords in M are
        # always followed by whitespace or newline.
        let_count = len(re.findall(r"\blet\b", expression))
        in_count = len(re.findall(r"\bin\b", expression))
        if let_count != in_count:
            raise ValueError(
                f"M expression has unbalanced 'let' ({let_count}) and "
                f"'in' ({in_count}) keywords."
            )
    elif "let" in expression and "in" not in expression:
        # A ``let`` with no ``in`` is not a complete M expression.
        raise ValueError("M expression contains 'let' but no matching 'in' keyword.")
    # No triple-quoted strings — TMDL's triple-quote delimiter
    # would clash.
    if "'''" in expression:
        raise ValueError(
            "M expression contains the triple-quote delimiter '''. "
            "Replace with three single quotes in a row inside an M "
            "string literal."
        )


# ---------------------------------------------------------------------------
# Source builders — return just the M expression string
# ---------------------------------------------------------------------------


def build_csv_source(
    path: str,
    *,
    delimiter: str = ",",
    has_headers: bool = True,
    encoding: str = "65001",
    quote_style: str = "QuoteStyle.Csv",
    extra_options: Optional[Sequence[str]] = None,
) -> str:
    """Build an M expression for a CSV file source.

    The result is a *staging* expression that returns a table. Wrap
    it with :func:`build_promoted_table` to get the typical
    promoted-header / changed-type pattern Power BI Desktop emits.
    """
    if not path:
        raise ValueError("CSV path must be non-empty.")
    options = [
        f"Delimiter={quote_string(delimiter)}",
        f"Encoding={encoding}",
        f"QuoteStyle={quote_style}",
    ]
    # When has_headers is true, CSV uses the first row as headers —
    # Power BI's Csv.Document helper signals that with a positional
    # argument ``null`` for the delimiter (no, actually with the
    # ``QuoteStyle.Csv`` flag). For now we keep the options array
    # minimal; the *promotion* is done by build_promoted_table.
    if extra_options:
        options.extend(extra_options)
    options_str = "[" + ", ".join(options) + "]"
    return (
        f"let\n"
        f"    Source = Csv.Document("
        f"File.Contents({quote_string(path)}), "
        f"{options_str})\n"
        f"in\n"
        f"    Source"
    )


def build_sql_source(
    server: str,
    database: str,
    query: str,
    *,
    privacy: Optional[str] = None,
) -> str:
    """Build an M expression for a SQL Server / Azure SQL source."""
    if not server:
        raise ValueError("SQL server must be non-empty.")
    if not database:
        raise ValueError("SQL database must be non-empty.")
    if not query:
        raise ValueError("SQL query must be non-empty.")
    options = [f"Query={quote_string(query)}"]
    if privacy:
        options.append(f"Privacy={privacy}")
    options_str = "[" + ", ".join(options) + "]"
    return (
        f"let\n"
        f"    Source = Sql.Database("
        f"{quote_string(server)}, {quote_string(database)}, "
        f"{options_str})\n"
        f"in\n"
        f"    Source"
    )


def build_json_source(path: str) -> str:
    """Build an M expression for a JSON file source."""
    if not path:
        raise ValueError("JSON path must be non-empty.")
    return (
        f"let\n"
        f"    Source = Json.Document(File.Contents({quote_string(path)}))\n"
        f"in\n"
        f"    Source"
    )


def build_sharepoint_source(site_url: str, file_path: str) -> str:
    """Build an M expression for a SharePoint Excel / CSV file source."""
    if not site_url:
        raise ValueError("SharePoint site URL must be non-empty.")
    if not file_path:
        raise ValueError("SharePoint file path must be non-empty.")
    return (
        f"let\n"
        f"    Source = SharePoint.Files("
        f"{quote_string(site_url)}, "
        f"{quote_string(file_path)})\n"
        f"in\n"
        f"    Source"
    )


def build_odata_source(url: str) -> str:
    """Build an M expression for an OData feed source."""
    if not url:
        raise ValueError("OData URL must be non-empty.")
    return (
        f"let\n" f"    Source = OData.Feed({quote_string(url)})\n" f"in\n" f"    Source"
    )


def build_web_source(url: str) -> str:
    """Build an M expression for a generic Web / REST source."""
    if not url:
        raise ValueError("Web URL must be non-empty.")
    return (
        f"let\n"
        f"    Source = Web.Contents({quote_string(url)})\n"
        f"in\n"
        f"    Source"
    )


# ---------------------------------------------------------------------------
# Transformation builders
# ---------------------------------------------------------------------------


def build_promoted_table(
    staging_query: str,
    table_name: str,
    column_types: Optional[Sequence[Mapping[str, str]]] = None,
) -> str:
    """Wrap a staging query in the standard promote-headers + change-type
    pattern that Power BI Desktop emits when a user clicks
    *Transform → Use First Row as Headers*.

    ``column_types`` is a list of ``{"name": str, "type": str}`` dicts
    where ``type`` is one of the Power Query primitive names
    (``"text"``, ``"Int64.Type"``, ``"number"``, ``"date"``,
    ``"datetime"``, ``"logical"``, ``"currency"``, etc.). If omitted,
    only the header promotion step is emitted.
    """
    if not staging_query.strip():
        raise ValueError("staging_query must be non-empty.")
    if not table_name:
        raise ValueError("table_name must be non-empty.")
    promoted_step = f'#"{table_name}"'
    lines = [
        "let",
        f"    Source = {staging_query.strip()}",
        f"    {promoted_step} = Table.PromoteHeaders("
        f"Source, [PromoteAllScalars=true])",
    ]
    # Rename so the user-visible table name matches what they asked.
    # The previous step is the same identifier — no rename needed.
    if column_types:
        type_steps: List[str] = []
        for col in column_types:
            if "name" not in col or "type" not in col:
                raise ValueError("column_types entries must include 'name' and 'type'.")
            type_steps.append(f'{{"{col["name"]}", {col["type"]}}}')
        type_list = "{" + ", ".join(type_steps) + "}"
        lines.append(
            f'    #"Changed Type" = Table.TransformColumnTypes('
            f"{promoted_step}, {type_list})"
        )
        final = '#"Changed Type"'
    else:
        final = promoted_step
    lines.append("in")
    lines.append(f"    {final}")
    return "\n".join(lines)


def build_merge_query(
    left_query: str,
    right_query: str,
    join_keys: Sequence[Mapping[str, str]],
    *,
    kind: str = "LeftOuter",
) -> str:
    """Build an M query that merges two staging tables on ``join_keys``.

    ``join_keys`` is a list of ``{"left": "ColA", "right": "ColB"}``
    dicts (single key most common).  ``kind`` is one of
    ``LeftOuter`` / ``RightOuter`` / ``Inner`` / ``FullOuter`` /
    ``LeftAnti`` / ``RightAnti``.
    """
    if not left_query.strip():
        raise ValueError("left_query must be non-empty.")
    if not right_query.strip():
        raise ValueError("right_query must be non-empty.")
    if not join_keys:
        raise ValueError("join_keys must be non-empty.")
    if kind not in {
        "LeftOuter",
        "RightOuter",
        "Inner",
        "FullOuter",
        "LeftAnti",
        "RightAnti",
    }:
        raise ValueError(f"Unsupported merge kind: {kind}")
    left_cols = ", ".join(quote_identifier(k["left"]) for k in join_keys)
    right_cols = ", ".join(quote_identifier(k["right"]) for k in join_keys)
    return (
        f"let\n"
        f"    Left = {left_query.strip()},\n"
        f"    Right = {right_query.strip()},\n"
        f"    Merged = Table.NestedJoin("
        f"Left, {{{left_cols}}}, Right, {{{right_cols}}}, "
        f'"MergedColumns", JoinKind.{kind})\n'
        f"in\n"
        f"    Merged"
    )


def build_append_query(queries: Sequence[str]) -> str:
    """Build an M query that appends N staging tables vertically."""
    if not queries:
        raise ValueError("append queries list must be non-empty.")
    parts = [q.strip() for q in queries]
    lines = ["let"]
    for idx, q in enumerate(parts):
        lines.append(f"    Q{idx} = {q},")
    combined_step = (
        "Combined = Table.Combine({"
        + ", ".join(f"Q{i}" for i in range(len(parts)))
        + "})"
    )
    lines.append(f"    {combined_step}")
    lines.append("in")
    lines.append("    Combined")
    return "\n".join(lines)


def build_group_aggregate(
    staging_query: str,
    group_cols: Sequence[str],
    aggregations: Sequence[Mapping[str, str]],
) -> str:
    """Build an M query that groups a table by ``group_cols`` and
    applies ``aggregations``.

    ``aggregations`` is a list of
    ``{"column": "Amount", "function": "Sum", "alias": "Total"}``
    dicts. ``function`` is one of ``Sum`` / ``Average`` / ``Count`` /
    ``Min`` / ``Max`` / ``Median`` / ``StandardDeviation`` /
    ``Variance`` / ``List.Count``.
    """
    if not staging_query.strip():
        raise ValueError("staging_query must be non-empty.")
    if not group_cols:
        raise ValueError("group_cols must be non-empty.")
    if not aggregations:
        raise ValueError("aggregations must be non-empty.")
    valid_funcs = {
        "Sum",
        "Average",
        "Count",
        "Min",
        "Max",
        "Median",
        "StandardDeviation",
        "Variance",
        "List.Count",
    }
    group_list = "{" + ", ".join(quote_identifier(c) for c in group_cols) + "}"
    agg_lines: List[str] = []
    for agg in aggregations:
        if "column" not in agg or "function" not in agg:
            raise ValueError(
                "aggregation entries must include 'column' and 'function'."
            )
        if agg["function"] not in valid_funcs:
            raise ValueError(
                f"Unsupported aggregation function: {agg['function']}. "
                f"Valid: {sorted(valid_funcs)}"
            )
        alias = agg.get("alias") or f"{agg['column']}_{agg['function']}"
        agg_lines.append(
            f'        {{"{alias}", each List.{agg["function"]}(['
            f'{quote_identifier(agg["column"])}]), type nullable}}'
        )
    agg_block = "{\n" + ",\n".join(agg_lines) + "\n    }"
    return (
        f"let\n"
        f"    Source = {staging_query.strip()},\n"
        f'    #"Grouped" = Table.Group(Source, {group_list}, {agg_block})\n'
        f"in\n"
        f'    #"Grouped"'
    )


# ---------------------------------------------------------------------------
# Template dispatcher
# ---------------------------------------------------------------------------


# Map from logical template name to the parameterised builder. Each
# entry returns a *full* M expression (with ``let ... in``). The
# handler in ``tmdl_engine.py`` dispatches on this map.
TEMPLATE_BUILDERS: Dict[str, Any] = {
    "csv": build_csv_source,
    "sql": build_sql_source,
    "json": build_json_source,
    "sharepoint": build_sharepoint_source,
    "odata": build_odata_source,
    "web": build_web_source,
}


def build_from_template(template: str, params: Mapping[str, Any]) -> str:
    """Dispatch to the appropriate ``TEMPLATE_BUILDERS[template]`` function.

    Raises ``ValueError`` for unknown templates.
    """
    if template not in TEMPLATE_BUILDERS:
        raise ValueError(
            f"Unknown Power Query template '{template}'. "
            f"Valid templates: {sorted(TEMPLATE_BUILDERS)}."
        )
    builder = TEMPLATE_BUILDERS[template]
    return builder(**dict(params))
