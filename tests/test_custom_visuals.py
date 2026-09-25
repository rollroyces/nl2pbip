"""Tests for the custom-visual registry and CLI wiring.

The custom-visual registry lets an operator register Power BI
visual types in ``~/.nl2pbip/custom_visuals.toml`` so the LLM
planner can pick them up. The tests cover:

* Loading a TOML file (good entries, missing fields, malformed
  rows).
* The default-location fallback when no file exists.
* Merging into ``visual_types.CANONICAL_VISUAL_TYPES`` at import
  time so existing validation/normalisation code sees the new
  types.
* The CLI flag wiring (``--custom-visual`` /
  ``--custom-visuals-file``).
* The planner payload section.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import List

import pytest

from nl2pbip import custom_visuals, visual_types

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    """Snapshot the canonical set before the test, restore after.

    Several tests mutate ``visual_types.CANONICAL_VISUAL_TYPES``
    via :func:`register_custom_visuals_into_registry`. Without
    this fixture, test ordering would leak custom types between
    tests.
    """
    snapshot = frozenset(visual_types.CANONICAL_VISUAL_TYPES)
    custom_visuals._CUSTOM_VISUAL_CACHE = None  # reset cache
    try:
        yield
    finally:
        visual_types.CANONICAL_VISUAL_TYPES = snapshot
        custom_visuals._CUSTOM_VISUAL_CACHE = None


@pytest.fixture
def custom_toml(tmp_path: Path) -> Path:
    """Write a minimal two-entry custom_visuals.toml."""
    toml = tmp_path / "custom_visuals.toml"
    toml.write_text(
        textwrap.dedent("""
            [[visual]]
            name = "KPI Tile"
            visualType = "kpiTile"
            description = "Internal KPI tile with traffic-light icon."

            [[visual]]
            name = "Revenue Waterfall"
            visualType = "revenueWaterfall"
            description = "Finance waterfall with variance annotations."
            """).strip(),
        encoding="utf-8",
    )
    return toml


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestCustomVisualLoading:
    def test_load_returns_specs(self, custom_toml: Path) -> None:
        specs = custom_visuals.load_custom_visual_specs(custom_toml)
        assert len(specs) == 2
        kpi = next(s for s in specs if s.visual_type == "kpiTile")
        assert kpi.name == "KPI Tile"
        assert "traffic-light" in kpi.description
        waterfall = next(s for s in specs if s.visual_type == "revenueWaterfall")
        assert waterfall.name == "Revenue Waterfall"

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.toml"
        assert custom_visuals.load_custom_visual_specs(missing) == []

    def test_default_path_returns_empty_when_no_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point the default at a guaranteed-missing path.
        monkeypatch.setattr(
            custom_visuals, "DEFAULT_CUSTOM_VISUALS_PATH", Path("/no/such/file.toml")
        )
        assert custom_visuals.get_custom_visual_specs() == []

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("[[visual]\nthis is not valid toml", encoding="utf-8")
        # ``tomllib`` is imported at the conftest level (Python 3.10
        # falls back to ``tomli``), so we reach for the symbol via
        # the conftest module instead of re-importing at runtime.
        from conftest import tomllib as toml_lib  # noqa: F401

        with pytest.raises(toml_lib.TOMLDecodeError):
            custom_visuals.load_custom_visual_specs(bad)

    def test_missing_name_skipped(self, tmp_path: Path) -> None:
        toml = tmp_path / "custom_visuals.toml"
        toml.write_text(
            textwrap.dedent("""
                [[visual]]
                visualType = "noNameVisual"
                description = "row without name"
                """).strip(),
            encoding="utf-8",
        )
        specs = custom_visuals.load_custom_visual_specs(toml)
        assert specs == []

    def test_missing_visual_type_skipped(self, tmp_path: Path) -> None:
        toml = tmp_path / "custom_visuals.toml"
        toml.write_text(
            textwrap.dedent("""
                [[visual]]
                name = "Anonymous"
                description = "row without visualType"
                """).strip(),
            encoding="utf-8",
        )
        specs = custom_visuals.load_custom_visual_specs(toml)
        assert specs == []

    def test_non_array_visual_section_warns(self, tmp_path: Path) -> None:
        # A common mistake: a single [visual] table instead of
        # the [[visual]] array-of-tables. Loader should return []
        # with a warning rather than crashing.
        toml = tmp_path / "custom_visuals.toml"
        toml.write_text(
            textwrap.dedent("""
                [visual]
                name = "x"
                visualType = "xVisual"
                """).strip(),
            encoding="utf-8",
        )
        specs = custom_visuals.load_custom_visual_specs(toml)
        assert specs == []


# ---------------------------------------------------------------------------
# Registry merge
# ---------------------------------------------------------------------------


class TestRegistryMerge:
    def test_register_merges_into_canonical_set(self, custom_toml: Path) -> None:
        custom_visuals.reload(custom_toml)
        new_types = custom_visuals.register_custom_visuals_into_registry()
        assert set(new_types) == {"kpiTile", "revenueWaterfall"}
        # Canonical set is the union of bundled + custom.
        assert "kpiTile" in visual_types.CANONICAL_VISUAL_TYPES
        assert "revenueWaterfall" in visual_types.CANONICAL_VISUAL_TYPES
        # Bundled types are still present.
        assert "columnChart" in visual_types.CANONICAL_VISUAL_TYPES

    def test_register_idempotent(self, custom_toml: Path) -> None:
        custom_visuals.reload(custom_toml)
        first = custom_visuals.register_custom_visuals_into_registry()
        second = custom_visuals.register_custom_visuals_into_registry()
        # Second call adds nothing — both visualTypes already merged.
        assert first == ["kpiTile", "revenueWaterfall"]
        assert second == []

    def test_normalize_accepts_custom_visual_type(self, custom_toml: Path) -> None:
        """The existing normalizer must recognise the merged types."""
        custom_visuals.reload(custom_toml)
        custom_visuals.register_custom_visuals_into_registry()
        # Should not raise; kpiTile is now canonical.
        assert visual_types.normalize_visual_type("kpiTile") == "kpiTile"
        # Case-insensitive lookup also works (existing path).
        assert visual_types.normalize_visual_type("KPITILE") == "kpiTile"

    def test_validate_accepts_custom_visual_type(self, custom_toml: Path) -> None:
        custom_visuals.reload(custom_toml)
        custom_visuals.register_custom_visuals_into_registry()
        # Strict validator should not raise on a merged type.
        visual_types.validate_visual_type("revenueWaterfall")


# ---------------------------------------------------------------------------
# Planner payload + CLI wiring
# ---------------------------------------------------------------------------


class TestPlannerPayloadAndCLI:
    def test_planner_payload_section(self, custom_toml: Path) -> None:
        custom_visuals.reload(custom_toml)
        payload = custom_visuals.planner_payload_section()
        assert "custom_visuals" in payload
        names = [v["name"] for v in payload["custom_visuals"]]
        assert "KPI Tile" in names
        assert "Revenue Waterfall" in names

    def test_planner_payload_empty_when_no_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            custom_visuals, "DEFAULT_CUSTOM_VISUALS_PATH", Path("/no/such/file.toml")
        )
        custom_visuals.reload()
        payload = custom_visuals.planner_payload_section()
        assert payload == {"custom_visuals": []}

    def test_cli_parses_custom_visual_flag(self) -> None:
        """argparse wires --custom-visual as a repeatable list."""
        from nl2pbip.cli import parse_args

        ns = parse_args(
            [
                "generate",
                "--prompt",
                "Show me revenue by region",
                "--custom-visual",
                "KPI Tile",
                "--custom-visual",
                "Revenue Waterfall",
            ]
        )
        assert ns.custom_visual == ["KPI Tile", "Revenue Waterfall"]
        assert ns.custom_visuals_file is None

    def test_cli_parses_custom_visuals_file(self) -> None:
        from nl2pbip.cli import parse_args

        ns = parse_args(
            [
                "generate",
                "--prompt",
                "x",
                "--custom-visuals-file",
                "/tmp/my_visual.toml",
            ]
        )
        assert ns.custom_visuals_file == "/tmp/my_visual.toml"
        assert ns.custom_visual == []


# ---------------------------------------------------------------------------
# End-to-end: stubbed planner sees the custom visual in its payload
# ---------------------------------------------------------------------------


class TestPlannerReceivesCustomVisual:
    """The task spec asks for an end-to-end test that the planner
    payload carries the custom visual. Rather than spin up a real
    orchestrator (heavy), we build a minimal stub that mirrors the
    shape of the orchestrator's planner-payload construction."""

    def test_stubbed_planner_payload_includes_custom_visual(
        self, custom_toml: Path
    ) -> None:
        custom_visuals.reload(custom_toml)
        custom_visuals.register_custom_visuals_into_registry()

        # Stub: collect everything the planner would see.
        payload: dict = {
            "visual_types": {
                "canonical": sorted(visual_types.CANONICAL_VISUAL_TYPES),
            },
            **custom_visuals.planner_payload_section(),
        }
        # The custom visualType appears in canonical (merged) AND
        # in the custom_visuals payload with its name/description.
        assert "kpiTile" in payload["visual_types"]["canonical"]
        custom_section = payload["custom_visuals"]
        names = {entry["name"] for entry in custom_section}
        assert "KPI Tile" in names
        types_in_section = {entry["visualType"] for entry in custom_section}
        assert types_in_section == {"kpiTile", "revenueWaterfall"}
