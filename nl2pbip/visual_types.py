"""Power BI visual-type handling.

Power BI Desktop accepts a wide range of visual types when authoring
``.pbir`` reports, but the canonical names aren't always obvious from
LLM-friendly descriptions:

* The "Table" visual in the Power BI Desktop UI is wired to the
  ``tableEx`` schema spelling (the legacy ``table`` spelling is still
  accepted by Desktop for backward-compatibility, but ``tableEx`` is
  what the engine writes).
* "Matrix" and "Pivot Table" are both names for the same visual;
  Power BI uses ``pivotTable`` internally.
* "Pie" / "Donut" / "Funnel" / "Treemap" are valid visual types but
  the previous validation layer rejected them.

This module centralises three concerns:

1. **Canonical visual types** — the spelling Power BI Desktop writes
   to ``.pbir`` files. Mirrors :class:`PBIRValidator._SUPPORTED_VISUALS`.
2. **Alias resolution** — common names an LLM (or a human describing
   the visual in natural language) might use. All aliases map to a
   canonical type.
3. **Layout hints** — category (``kpi`` / ``chart`` / ``detail``) and
   default on-screen size, used by the :class:`LayoutManager` to place
   new visuals when the caller doesn't supply explicit coordinates.

Public API:
* :func:`normalize_visual_type` — alias → canonical mapping; raises
  :class:`VisualTypeError` for unrecognised inputs.
* :func:`validate_visual_type` — strict preflight check (no alias
  rewrite). Use this in the LLM retry-loop feedback path so the
  caller learns the canonical spelling.
* :func:`category_for_visual_type` — layout category lookup.
* :func:`default_size_for_visual_type` — default on-screen size.
* :data:`CANONICAL_VISUAL_TYPES` — the canonical set.
* :data:`VISUAL_TYPE_ALIASES` — alias → canonical mapping.
"""

from __future__ import annotations

from typing import Tuple

# ---------------------------------------------------------------------------
# Canonical visual types
# ---------------------------------------------------------------------------

CANONICAL_VISUAL_TYPES = frozenset(
    {
        # KPI / single-value
        "card",
        "kpi",
        "multiRowCard",
        # Slicers / filters
        "slicer",
        # Charts
        "barChart",
        "columnChart",
        "lineChart",
        "areaChart",
        "scatterChart",
        "pieChart",
        "donutChart",
        "funnelChart",
        "ribbonChart",
        "waterfallChart",
        "treemap",
        # Detail / tabular
        "tableEx",
        "pivotTable",
    }
)

# ---------------------------------------------------------------------------
# Aliases — friendly / LLM-emitted names → canonical type
# ---------------------------------------------------------------------------

VISUAL_TYPE_ALIASES: dict[str, str] = {
    # LLM-friendly names
    "table": "tableEx",
    "tabulardetail": "tableEx",
    "dataTable": "tableEx",
    # Matrix is the Power BI Desktop UI name; pivotTable is the schema name
    "matrix": "pivotTable",
    "pivot": "pivotTable",
    "pivottable": "pivotTable",
    # "Pie" / "donut" — same visual under different names
    "pie": "pieChart",
    "donut": "donutChart",
    "doughnut": "donutChart",
    # Common synonym pairs
    "bar": "barChart",
    "barchart": "barChart",
    "column": "columnChart",
    "columnchart": "columnChart",
    "line": "lineChart",
    "linechart": "lineChart",
    "area": "areaChart",
    "areachart": "areaChart",
    "scatter": "scatterChart",
    "scatterchart": "scatterChart",
    "funnel": "funnelChart",
    "treemap": "treemap",  # canonical, but accept lowercase
    # KPI cards
    "kpicard": "kpi",
    "cardVisual": "card",
}


# ---------------------------------------------------------------------------
# Layout hints — category and default size for each canonical type.
# ---------------------------------------------------------------------------

VISUAL_CATEGORY = {
    # KPI
    "card": "kpi",
    "kpi": "kpi",
    "multiRowCard": "kpi",
    # Slicers share the KPI row
    "slicer": "kpi",
    # Charts
    "barChart": "chart",
    "columnChart": "chart",
    "lineChart": "chart",
    "areaChart": "chart",
    "scatterChart": "chart",
    "pieChart": "chart",
    "donutChart": "chart",
    "funnelChart": "chart",
    "ribbonChart": "chart",
    "waterfallChart": "chart",
    "treemap": "chart",
    # Detail
    "tableEx": "detail",
    "pivotTable": "detail",
}

DEFAULT_SIZES: dict[str, Tuple[float, float]] = {
    "card": (220, 140),
    "kpi": (220, 140),
    "multiRowCard": (300, 200),
    "slicer": (220, 200),
    "barChart": (420, 260),
    "columnChart": (420, 260),
    "lineChart": (420, 260),
    "areaChart": (420, 260),
    "scatterChart": (420, 260),
    "pieChart": (360, 320),
    "donutChart": (360, 320),
    "funnelChart": (420, 320),
    "ribbonChart": (420, 260),
    "waterfallChart": (420, 280),
    "treemap": (420, 320),
    "tableEx": (560, 220),
    "pivotTable": (560, 320),
}


class VisualTypeError(ValueError):
    """Raised when a visual type is not recognised.

    Inherits ``ValueError`` so existing ``except ValueError`` blocks
    still catch it; the dedicated class lets the orchestrator's LLM
    retry loop surface a richer error.
    """


def _resolve_case_insensitive(visual_type: str) -> str | None:
    """Find a matching canonical type ignoring case.

    Returns the canonical spelling if a case-insensitive match exists
    in either :data:`CANONICAL_VISUAL_TYPES` or
    :data:`VISUAL_TYPE_ALIASES`. Returns ``None`` if no match.
    """
    lower = visual_type.lower()
    for candidate in CANONICAL_VISUAL_TYPES:
        if candidate.lower() == lower:
            return candidate
    for alias, canonical in VISUAL_TYPE_ALIASES.items():
        if alias.lower() == lower:
            return canonical
    return None


def validate_visual_type(visual_type: str) -> None:
    """Raise :class:`VisualTypeError` if ``visual_type`` is unknown.

    Strict preflight check — aliases are NOT accepted. Use this in the
    LLM retry-loop feedback path so callers learn the canonical
    spelling. For lenient resolution that maps aliases to canonical,
    use :func:`normalize_visual_type`.
    """
    if not isinstance(visual_type, str):
        raise VisualTypeError(
            f"visualType must be a string, got {type(visual_type).__name__}."
        )
    if visual_type in CANONICAL_VISUAL_TYPES:
        return
    canonical = _resolve_case_insensitive(visual_type)
    if canonical is not None:
        if canonical != visual_type:
            # Either a case mismatch on a canonical name, or an alias.
            if visual_type.lower() in {a.lower() for a in VISUAL_TYPE_ALIASES}:
                raise VisualTypeError(
                    f"visualType {visual_type!r} is an alias for "
                    f"{canonical!r}; use the canonical spelling."
                )
            raise VisualTypeError(
                f"visualType {visual_type!r} is the wrong case; use " f"{canonical!r}."
            )
        return
    raise VisualTypeError(
        f"Unknown visualType {visual_type!r}. "
        f"Canonical types: {sorted(CANONICAL_VISUAL_TYPES)}. "
        f"Aliases (rewrite to canonical automatically): "
        f"{sorted(VISUAL_TYPE_ALIASES)}."
    )


def normalize_visual_type(
    visual_type: str,
    *,
    on_unknown: str = "raise",
) -> str:
    """Return the canonical spelling for ``visual_type``.

    Parameters
    ----------
    visual_type:
        Input spelling — canonical, alias, or unknown.
    on_unknown:
        * ``"raise"`` (default) — :class:`VisualTypeError` for unrecognised input.
        * ``"fallback"`` — return ``"tableEx"`` for any unknown input. Use
          only in read/validate paths where you want to be lenient.
    """
    if not isinstance(visual_type, str):
        if on_unknown == "fallback":
            return "tableEx"
        raise VisualTypeError(
            f"visualType must be a string, got {type(visual_type).__name__}."
        )
    if visual_type in CANONICAL_VISUAL_TYPES:
        return visual_type
    canonical = _resolve_case_insensitive(visual_type)
    if canonical is not None:
        return canonical
    if on_unknown == "fallback":
        return "tableEx"
    # Reach the rich error message from the validator.
    validate_visual_type(visual_type)
    # Defensive: validate_visual_type should have raised already.
    raise VisualTypeError(f"Unknown visualType {visual_type!r}.")  # pragma: no cover


def category_for_visual_type(visual_type: str) -> str:
    """Return the layout category for a visual type (``kpi`` / ``chart`` / ``detail``).

    Unknown visual types are coerced to ``tableEx`` (which falls in the
    ``detail`` row) so the layout engine always has a sensible default.
    """
    canonical = normalize_visual_type(visual_type, on_unknown="fallback")
    return VISUAL_CATEGORY.get(canonical, "detail")


def default_size_for_visual_type(visual_type: str) -> Tuple[float, float]:
    """Return ``(width, height)`` default in pixels for a visual type."""
    canonical = normalize_visual_type(visual_type, on_unknown="fallback")
    return DEFAULT_SIZES.get(canonical, (420, 260))


__all__ = [
    "CANONICAL_VISUAL_TYPES",
    "DEFAULT_SIZES",
    "VISUAL_CATEGORY",
    "VISUAL_TYPE_ALIASES",
    "VisualTypeError",
    "category_for_visual_type",
    "default_size_for_visual_type",
    "normalize_visual_type",
    "validate_visual_type",
]
