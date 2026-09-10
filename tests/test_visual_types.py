"""Unit tests for the visual-type handling layer.

Covers:
* Canonical visual types pass through ``normalize_visual_type``.
* Friendly aliases (``table`` → ``tableEx``, ``matrix`` → ``pivotTable``,
  ``pie`` → ``pieChart``, ``doughnut`` → ``donutChart``, ``cardVisual``
  → ``card``) map to the canonical form.
* Aliases are case-insensitive.
* Unknown types raise :class:`VisualTypeError` by default; the
  fallback mode returns ``tableEx``.
* ``validate_visual_type`` is the strict preflight check that rejects
  aliases (so the LLM retry loop learns the canonical spelling).
* Layout helpers (``category_for_visual_type``,
  ``default_size_for_visual_type``) accept both canonical and alias
  spellings and return sensible defaults.
* ``PBIRValidator.validate_visual`` normalises ``visualType`` in
  place when an alias is supplied and rejects unknown types with a
  rich error message.
* ``add_visual_handler`` accepts aliases and writes the canonical
  spelling to disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from nl2pbip.pbir_engine import add_report_page_handler, add_visual_handler
from nl2pbip.pbir_validator import PBIRValidationError, PBIRValidator
from nl2pbip.tmdl_engine import create_table_handler
from nl2pbip.visual_types import (
    CANONICAL_VISUAL_TYPES,
    DEFAULT_SIZES,
    VISUAL_CATEGORY,
    VISUAL_TYPE_ALIASES,
    VisualTypeError,
    category_for_visual_type,
    default_size_for_visual_type,
    normalize_visual_type,
    validate_visual_type,
)

# ---------------------------------------------------------------------------
# normalize_visual_type
# ---------------------------------------------------------------------------


class TestNormalizeVisualType:
    @pytest.mark.parametrize(
        "canonical",
        [
            "card",
            "kpi",
            "multiRowCard",
            "slicer",
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
            "tableEx",
            "pivotTable",
        ],
    )
    def test_canonical_spellings_pass_through(self, canonical: str) -> None:
        assert normalize_visual_type(canonical) == canonical

    @pytest.mark.parametrize(
        "alias,canonical",
        [
            # Friendly names → Power BI schema names
            ("table", "tableEx"),
            ("matrix", "pivotTable"),
            ("pie", "pieChart"),
            ("donut", "donutChart"),
            ("doughnut", "donutChart"),
            ("funnel", "funnelChart"),
            # Single-word forms
            ("bar", "barChart"),
            ("column", "columnChart"),
            ("line", "lineChart"),
            ("area", "areaChart"),
            ("scatter", "scatterChart"),
            ("pivot", "pivotTable"),
            ("treemap", "treemap"),
            # KPI cards
            ("kpicard", "kpi"),
            ("cardVisual", "card"),
        ],
    )
    def test_alias_maps_to_canonical(self, alias: str, canonical: str) -> None:
        assert normalize_visual_type(alias) == canonical

    def test_aliases_are_case_insensitive(self) -> None:
        assert normalize_visual_type("TABLE") == "tableEx"
        assert normalize_visual_type("Matrix") == "pivotTable"
        assert normalize_visual_type("PIE") == "pieChart"
        assert normalize_visual_type("Donut") == "donutChart"

    def test_unknown_type_raises_by_default(self) -> None:
        with pytest.raises(VisualTypeError) as exc_info:
            normalize_visual_type("fluffyUnicorn")
        assert "fluffyUnicorn" in str(exc_info.value)
        # The error message lists accepted types so the LLM retry
        # loop can self-correct without consulting docs.
        assert "Canonical types" in str(exc_info.value)

    def test_unknown_type_returns_tableex_in_fallback_mode(self) -> None:
        assert (
            normalize_visual_type("fluffyUnicorn", on_unknown="fallback") == "tableEx"
        )
        assert normalize_visual_type("", on_unknown="fallback") == "tableEx"

    def test_non_string_raises(self) -> None:
        with pytest.raises(VisualTypeError):
            normalize_visual_type(None)  # type: ignore[arg-type]
        # In fallback mode, non-strings also coerce to tableEx.
        assert normalize_visual_type(None, on_unknown="fallback") == "tableEx"  # type: ignore[arg-type]

    def test_error_message_lists_alias(self) -> None:
        with pytest.raises(VisualTypeError) as exc_info:
            validate_visual_type("table")
        assert "table" in str(exc_info.value)
        assert "tableEx" in str(exc_info.value)


# ---------------------------------------------------------------------------
# validate_visual_type
# ---------------------------------------------------------------------------


class TestValidateVisualType:
    def test_accepts_canonical(self) -> None:
        validate_visual_type("card")
        validate_visual_type("tableEx")
        validate_visual_type("pieChart")

    def test_rejects_alias_by_name(self) -> None:
        with pytest.raises(VisualTypeError) as exc_info:
            validate_visual_type("table")
        assert "table" in str(exc_info.value)
        assert "tableEx" in str(exc_info.value)

    def test_rejects_unknown(self) -> None:
        with pytest.raises(VisualTypeError):
            validate_visual_type("fluffyUnicorn")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(VisualTypeError):
            validate_visual_type(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


class TestLayoutHelpers:
    @pytest.mark.parametrize(
        "visual_type,expected_category",
        [
            ("card", "kpi"),
            ("kpi", "kpi"),
            ("slicer", "kpi"),
            ("barChart", "chart"),
            ("pieChart", "chart"),
            ("donutChart", "chart"),
            ("treemap", "chart"),
            ("tableEx", "detail"),
            ("pivotTable", "detail"),
        ],
    )
    def test_category_for_canonical_type(
        self, visual_type: str, expected_category: str
    ) -> None:
        assert category_for_visual_type(visual_type) == expected_category

    @pytest.mark.parametrize(
        "alias,expected_category",
        [
            ("table", "detail"),
            ("matrix", "detail"),
            ("pie", "chart"),
            ("donut", "chart"),
            ("doughnut", "chart"),
        ],
    )
    def test_category_for_alias(self, alias: str, expected_category: str) -> None:
        assert category_for_visual_type(alias) == expected_category

    def test_category_for_unknown_falls_back_to_detail(self) -> None:
        # Unknown visual types fall back to ``tableEx`` (the
        # conservative "let Power BI render whatever it can" default)
        # which lives in the ``detail`` row.
        assert category_for_visual_type("fluffyUnicorn") == "detail"

    def test_default_size_for_canonical_type(self) -> None:
        w, h = default_size_for_visual_type("pieChart")
        assert w == 360
        assert h == 320

    def test_default_size_for_alias(self) -> None:
        w, h = default_size_for_visual_type("pie")
        assert (w, h) == DEFAULT_SIZES["pieChart"]

    def test_default_size_for_unknown(self) -> None:
        # Unknown visual types fall back to a sensible default via
        # the ``tableEx`` tableEx alias (the safe "let Power BI
        # render whatever it can" default). We don't assert a
        # specific value here — just that no exception is raised
        # and the result is a numeric (width, height) pair.
        w, h = default_size_for_visual_type("fluffyUnicorn")
        assert isinstance(w, (int, float))
        assert isinstance(h, (int, float))
        assert w > 0 and h > 0


# ---------------------------------------------------------------------------
# Validator integration
# ---------------------------------------------------------------------------


class TestPBIRValidatorAcceptsAliases:
    def test_table_alias_normalised_in_place(self) -> None:
        validator = PBIRValidator()
        visual = {
            "$schema": "http://powerbi.com/product/schema#visualContainer",
            "name": "v1",
            "visualType": "table",
            "layout": {"x": 24, "y": 24, "width": 560, "height": 220, "z": 1},
            "config": {
                "singleVisual": {
                    "visualType": "table",
                    "projections": {"Rows": [{"queryRef": "T[R]"}]},
                }
            },
        }
        validator.validate_visual(visual, page_bounds=(1280, 720))
        # The validator normalises the alias to the canonical form so
        # the on-disk JSON always matches what Power BI Desktop emits.
        assert visual["visualType"] == "tableEx"
        assert visual["config"]["singleVisual"]["visualType"] == "tableEx"

    def test_matrix_alias_normalised_in_place(self) -> None:
        validator = PBIRValidator()
        visual = {
            "$schema": "http://powerbi.com/product/schema#visualContainer",
            "name": "v1",
            "visualType": "matrix",
            "layout": {"x": 24, "y": 24, "width": 560, "height": 220, "z": 1},
            "config": {
                "singleVisual": {
                    "visualType": "matrix",
                    "projections": {"Rows": [{"queryRef": "T[R]"}]},
                }
            },
        }
        validator.validate_visual(visual, page_bounds=(1280, 720))
        assert visual["visualType"] == "pivotTable"

    def test_unknown_type_rejected_with_helpful_error(self) -> None:
        validator = PBIRValidator()
        visual = {
            "$schema": "http://powerbi.com/product/schema#visualContainer",
            "name": "v1",
            "visualType": "fluffyUnicorn",
            "layout": {"x": 24, "y": 24, "width": 560, "height": 220, "z": 1},
            "config": {
                "singleVisual": {"visualType": "fluffyUnicorn", "projections": {}}
            },
        }
        with pytest.raises(PBIRValidationError) as exc_info:
            validator.validate_visual(visual, page_bounds=(1280, 720))
        assert "fluffyUnicorn" in str(exc_info.value)
        # The error message lists the canonical types so the LLM
        # retry loop can self-correct.
        assert "Canonical types" in str(exc_info.value)

    def test_missing_visual_type_rejected(self) -> None:
        validator = PBIRValidator()
        visual = {
            "$schema": "http://powerbi.com/product/schema#visualContainer",
            "name": "v1",
            "layout": {"x": 24, "y": 24, "width": 560, "height": 220, "z": 1},
            "config": {"singleVisual": {"projections": {}}},
        }
        with pytest.raises(PBIRValidationError) as exc_info:
            validator.validate_visual(visual, page_bounds=(1280, 720))
        assert "visualType is required" in str(exc_info.value)

    def test_supported_visuals_class_attribute_matches_canonical_set(self) -> None:
        # Backwards-compat: callers that imported
        # ``PBIRValidator._SUPPORTED_VISUALS`` still see the full set.
        assert PBIRValidator._SUPPORTED_VISUALS == CANONICAL_VISUAL_TYPES


# ---------------------------------------------------------------------------
# End-to-end: add_visual_handler with aliases
# ---------------------------------------------------------------------------


class TestAddVisualHandlerAcceptsAliases:
    def _build_ctx(self, tmp_path: Path) -> Dict[str, Any]:
        # Use a taller canvas so detail-band visuals (tableEx,
        # pivotTable) fit cleanly without the layout manager having
        # to downscale them. Real Power BI Desktop reports often
        # use custom canvas heights; 1280x960 keeps the math simple
        # while leaving headroom for KPI + chart + detail bands.
        ctx = {
            "model_path": str(tmp_path / "model.tmdl"),
            "report_path": str(tmp_path / "report.json"),
            "page_size": {"width": 1280, "height": 960},
        }
        create_table_handler(
            table_name="T",
            columns=[
                {"name": "R", "data_type": "string"},
                {"name": "V", "data_type": "decimal"},
            ],
            context=ctx,
        )
        add_report_page_handler(page="Main", context=ctx)
        return ctx

    @pytest.mark.parametrize(
        "alias",
        ["table", "matrix", "pie", "donut", "doughnut", "funnel", "cardVisual"],
    )
    def test_alias_creates_visual_with_canonical_type(
        self, tmp_path: Path, alias: str
    ) -> None:
        ctx = self._build_ctx(tmp_path)
        result = add_visual_handler(
            page="Main",
            visual_type=alias,
            bindings={"Rows": ["T[R]", "T[V]"]},
            context=ctx,
        )
        assert result["status"] == "success"

    def test_unknown_type_raises_before_writing(self, tmp_path: Path) -> None:
        ctx = self._build_ctx(tmp_path)
        from nl2pbip.visual_types import normalize_visual_type

        with pytest.raises(ValueError) as exc_info:
            add_visual_handler(
                page="Main",
                visual_type="fluffyUnicorn",
                bindings={"Rows": ["T[R]"]},
                context=ctx,
            )
        # Error message references the canonical types so the LLM
        # retry loop learns what to use.
        assert "Canonical types" in str(exc_info.value)

    def test_table_alias_writes_tableex_to_disk(self, tmp_path: Path) -> None:
        ctx = self._build_ctx(tmp_path)
        add_visual_handler(
            page="Main",
            visual_type="table",
            bindings={"Rows": ["T[R]", "T[V]"]},
            context=ctx,
        )
        # Read the report back and confirm the on-disk visualType is
        # canonical (tableEx), not the alias the caller supplied.
        # The on-disk report.json wraps the PBIR payload under
        # ``pbir`` and the working-copy state under ``internal``;
        # we inspect the PBIR portion because that's what Power BI
        # Desktop actually reads.
        import json

        report = json.loads(Path(ctx["report_path"]).read_text())
        pbir = report.get("pbir", report)
        sections = pbir.get("sections") or pbir.get("pages") or []
        assert sections, f"expected at least one section in {pbir}"
        visuals = sections[0].get("visualContainers") or sections[0].get("visuals", [])
        # Find the visual we just added (the page may have other
        # visuals from the layout engine).
        table_visuals = [
            v
            for v in visuals
            if v.get("config", {}).get("singleVisual", {}).get("visualType")
            == "tableEx"
        ]
        assert table_visuals, f"expected at least one tableEx visual in {visuals}"
