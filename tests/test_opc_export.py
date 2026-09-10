"""OPC compliance tests for the .pbit exporter.

These tests verify the exporter produces archives that follow the
Open Packaging Conventions Power BI Desktop expects. The canonical
structure (verified by inspecting Microsoft's official .pbit
templates) is:

* ``[Content_Types].xml`` at the archive root, UTF-8 with BOM.
* Forward-slash paths (``Report/Layout``, not ``Report\\Layout``).
* No ``_rels/.rels`` (Power BI doesn't use OPC package rels).
* Manifest parts at the root: ``Version``, ``Metadata``, ``Settings``,
  ``SecurityBindings``, ``DiagramLayout``.
* Model parts at the root: ``DataModelSchema``, ``DataMashup``.
* Report parts under ``Report/``: ``Report/Layout``,
  ``Report/LinguisticSchema``, optional ``Report/StaticResources/``
  and ``Report/CustomVisuals/``.
* All Override entries use the OPC namespace
  ``http://schemas.openxmlformats.org/package/2006/content-types``.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import List

import pytest

from nl2pbip.exporter.exporter import PBIPExporter, _extract_project_name
from nl2pbip.exporter.opc import (
    DEFAULT_COMPATIBILITY_LEVEL,
    DEFAULT_CULTURE,
    _ContentTypeRegistry,
    make_data_mashup_stub,
    make_diagram_layout_json,
    make_metadata_json,
    make_minimal_data_model_schema,
    make_security_bindings_json,
    make_settings_json,
    make_version_part,
)
from nl2pbip.exporter.pbit_builder import (
    PbitArchiveBuilder,
    _build_report_layout_from_pbip,
    _find_component_dir,
    _match_glob,
)

# ---------------------------------------------------------------------------
# Content-type registry
# ---------------------------------------------------------------------------


class TestContentTypeRegistry:
    def test_default_extensions_present(self) -> None:
        registry = _ContentTypeRegistry.for_power_bi()
        for ext in ("json", "xml", "png", "jpg", "svg"):
            assert ext in registry.defaults

    def test_known_overrides_present(self) -> None:
        registry = _ContentTypeRegistry.for_power_bi()
        # Canonical Power BI Override entries for known parts.
        for path in (
            "Version",
            "Metadata",
            "Settings",
            "SecurityBindings",
            "DiagramLayout",
            "DataModelSchema",
            "DataMashup",
            "Report/Layout",
            "Report/LinguisticSchema",
        ):
            assert path in registry.overrides, f"missing Override for {path}"

    def test_add_override(self) -> None:
        registry = _ContentTypeRegistry()
        registry.add_override("Custom/MyPart", "application/x-custom")
        assert registry.overrides["Custom/MyPart"] == "application/x-custom"

    def test_to_xml_emits_utf8_bom(self) -> None:
        registry = _ContentTypeRegistry.for_power_bi()
        body = registry.to_xml()
        # UTF-8 BOM is mandatory — Power BI Desktop rejects archives
        # without it.
        assert body.startswith(b"\xef\xbb\xbf")

    def test_to_xml_uses_opc_namespace(self) -> None:
        registry = _ContentTypeRegistry.for_power_bi()
        body = registry.to_xml()
        # Strip BOM before string search.
        text = body.lstrip(b"\xef\xbb\xbf").decode("utf-8")
        assert (
            'xmlns="http://schemas.openxmlformats.org/package/2006/content-types"'
            in text
        )

    def test_to_xml_emits_xml_declaration(self) -> None:
        registry = _ContentTypeRegistry()
        text = registry.to_xml().lstrip(b"\xef\xbb\xbf").decode("utf-8")
        assert text.startswith('<?xml version="1.0" encoding="utf-8"?>')

    def test_to_xml_contains_added_default(self) -> None:
        registry = _ContentTypeRegistry()
        registry.add_default("xyz", "application/x-custom")
        text = registry.to_xml().lstrip(b"\xef\xbb\xbf").decode("utf-8")
        assert '<Default Extension="xyz" ContentType="application/x-custom"' in text

    def test_to_xml_contains_added_override(self) -> None:
        registry = _ContentTypeRegistry()
        registry.add_override("Custom", "application/x-thing")
        text = registry.to_xml().lstrip(b"\xef\xbb\xbf").decode("utf-8")
        assert '<Override PartName="/Custom" ContentType="application/x-thing"' in text


# ---------------------------------------------------------------------------
# Manifest payload helpers
# ---------------------------------------------------------------------------


class TestManifestHelpers:
    def test_version_part_is_one_byte(self) -> None:
        assert make_version_part() == b"1"

    def test_data_mashup_stub_has_eight_byte_header(self) -> None:
        body = make_data_mashup_stub()
        # The 8-byte magic header is required for Power BI Desktop
        # to recognise the part.
        assert len(body) == 8
        assert body[:4] == b"\x01\x00\x00\x00"
        assert body[4:] == b"\x01\x00\x00\x00"

    def test_minimal_data_model_schema_has_required_fields(self) -> None:
        schema = make_minimal_data_model_schema()
        assert schema["compatibilityLevel"] == DEFAULT_COMPATIBILITY_LEVEL
        assert schema["model"]["culture"] == DEFAULT_CULTURE
        assert schema["model"]["defaultMode"] == "import"

    def test_metadata_json_has_required_fields(self) -> None:
        meta = make_metadata_json(title="T", description="D", created="2026")
        for key in ("title", "description", "created", "generator"):
            assert key in meta
        assert meta["generator"] == "nl2pbip"

    def test_settings_json_is_dict(self) -> None:
        s = make_settings_json()
        assert isinstance(s, dict)
        assert "defaultDrillThroughOther" in s

    def test_security_bindings_json_has_empty_role_bindings(self) -> None:
        s = make_security_bindings_json()
        assert s["roleBindings"] == []

    def test_diagram_layout_json_has_empty_diagrams(self) -> None:
        d = make_diagram_layout_json()
        assert d["diagrams"] == []


# ---------------------------------------------------------------------------
# PbitArchiveBuilder
# ---------------------------------------------------------------------------


class TestPbitArchiveBuilderSmoke:
    def _empty_pbip(self, tmp_path: Path) -> Path:
        pbip_dir = tmp_path / "TestProj.pbipdir"
        sm = pbip_dir / "TestProj.SemanticModel" / "definition" / "tables"
        sr = pbip_dir / "TestProj.Report" / "definition" / "pages" / "Main" / "visuals"
        sm.mkdir(parents=True)
        sr.mkdir(parents=True)

        (pbip_dir / "TestProj.pbip").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "name": "TestProj",
                    "components": [
                        {"type": "semanticModel", "path": "TestProj.SemanticModel"},
                        {"type": "report", "path": "TestProj.Report"},
                    ],
                }
            )
        )
        (pbip_dir / "TestProj.SemanticModel" / "definition.pbism").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "name": "TestProj",
                    "modelDefinition": "definition/model.tmdl",
                }
            )
        )
        (pbip_dir / "TestProj.Report" / "definition.pbir").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "name": "TestProj",
                    "reportDefinition": "definition/report.json",
                }
            )
        )
        (pbip_dir / "TestProj.SemanticModel" / "definition" / "model.tmdl").write_text(
            "table 'X' {\n  column 'id' { dataType: int64 }\n}"
        )
        (
            pbip_dir / "TestProj.SemanticModel" / "definition" / "relationships.tmdl"
        ).write_text("")
        (pbip_dir / "TestProj.Report" / "definition" / "report.json").write_text(
            json.dumps(
                {
                    "pages": [
                        {
                            "name": "Main",
                            "displayName": "Main",
                            "pageSize": {"width": 1280, "height": 720},
                        }
                    ],
                    "settings": {},
                }
            )
        )
        return pbip_dir

    def test_add_pbip_folder_emits_canonical_manifest(self, tmp_path: Path) -> None:
        pbip_dir = self._empty_pbip(tmp_path)
        out = tmp_path / "TestProj.pbit"

        builder = PbitArchiveBuilder(output_path=out, template_name="TestProj")
        builder.add_pbip_folder(pbip_dir)
        builder.finalize()

        assert out.exists()
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            # Required manifest parts at the archive root.
            assert "Version" in names
            assert "Metadata" in names
            assert "Settings" in names
            assert "SecurityBindings" in names
            assert "DiagramLayout" in names
            assert "DataModelSchema" in names
            assert "DataMashup" in names
            assert "[Content_Types].xml" in names
            # Report layout at the canonical forward-slash path.
            assert "Report/Layout" in names
            # No backslash paths (a Windows zip artifact).
            assert not any("\\" in n for n in names)

    def test_archive_has_utf8_bom_on_content_types(self, tmp_path: Path) -> None:
        pbip_dir = self._empty_pbip(tmp_path)
        out = tmp_path / "TestProj.pbit"

        builder = PbitArchiveBuilder(output_path=out)
        builder.add_pbip_folder(pbip_dir)
        builder.finalize()

        with zipfile.ZipFile(out) as z:
            ct = z.read("[Content_Types].xml")
            assert ct.startswith(b"\xef\xbb\xbf")

    def test_metadata_carries_template_name(self, tmp_path: Path) -> None:
        pbip_dir = self._empty_pbip(tmp_path)
        out = tmp_path / "TestProj.pbit"

        builder = PbitArchiveBuilder(output_path=out, template_name="CustomName")
        builder.add_pbip_folder(pbip_dir)
        builder.finalize()

        with zipfile.ZipFile(out) as z:
            meta = json.loads(z.read("Metadata").decode("utf-8"))
            assert meta["title"] == "CustomName"

    def test_no_rels_part_emitted(self, tmp_path: Path) -> None:
        """Power BI archives don't use OPC package relationships."""
        pbip_dir = self._empty_pbip(tmp_path)
        out = tmp_path / "TestProj.pbit"

        builder = PbitArchiveBuilder(output_path=out)
        builder.add_pbip_folder(pbip_dir)
        builder.finalize()

        with zipfile.ZipFile(out) as z:
            assert not any(
                n.startswith("_rels/") or n == "_rels/.rels" for n in z.namelist()
            )

    def test_static_resources_carried_through(self, tmp_path: Path) -> None:
        pbip_dir = self._empty_pbip(tmp_path)
        static_resources = (
            pbip_dir / "TestProj.Report" / "StaticResources" / "SharedResources"
        )
        static_resources.mkdir(parents=True)
        (static_resources / "theme.json").write_text("{}")

        out = tmp_path / "TestProj.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        builder.add_pbip_folder(pbip_dir)
        builder.finalize()

        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            assert "Report/StaticResources/SharedResources/theme.json" in names

    def test_custom_visuals_carried_through(self, tmp_path: Path) -> None:
        pbip_dir = self._empty_pbip(tmp_path)
        custom_visuals = (
            pbip_dir / "TestProj.Report" / "CustomVisuals" / "Gantt123" / "resources"
        )
        custom_visuals.mkdir(parents=True)
        (custom_visuals.parent / "package.json").write_text("{}")
        (custom_visuals / "Gantt123.pbiviz.json").write_text("{}")

        out = tmp_path / "TestProj.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        builder.add_pbip_folder(pbip_dir)
        builder.finalize()

        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            assert "Report/CustomVisuals/Gantt123/package.json" in names
            assert (
                "Report/CustomVisuals/Gantt123/resources/Gantt123.pbiviz.json" in names
            )


class TestPbitArchiveBuilderAPI:
    def test_add_part_normalises_backslashes(self, tmp_path: Path) -> None:
        """Path separators are always forward slashes (OPC requirement)."""
        out = tmp_path / "X.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        builder.add_part("Report\\Layout", b"{}")
        assert "Report/Layout" in builder.parts

    def test_add_part_rejects_empty_path(self, tmp_path: Path) -> None:
        out = tmp_path / "X.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        with pytest.raises(ValueError):
            builder.add_part("", b"{}")

    def test_add_part_rejects_leading_slash(self, tmp_path: Path) -> None:
        out = tmp_path / "X.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        # Leading slashes are normalised away.
        builder.add_part("/MyPart", b"{}")
        assert "MyPart" in builder.parts

    def test_add_part_rejects_modification_after_finalize(self, tmp_path: Path) -> None:
        out = tmp_path / "X.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        builder.finalize()
        with pytest.raises(RuntimeError):
            builder.add_part("AnotherPart", b"x")

    def test_ensure_manifest_parts_only_adds_missing(self, tmp_path: Path) -> None:
        out = tmp_path / "X.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        # Pre-set one part so it isn't overwritten.
        builder.add_version(b"99")
        builder.ensure_manifest_parts()
        assert builder.parts["Version"] == b"99"

    def test_add_directory_recursive(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.json").write_text("1")
        (src / "sub" / "b.json").write_text("2")
        out = tmp_path / "X.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        builder.add_directory(src, prefix="Report")
        assert "Report/a.json" in builder.parts
        assert "Report/sub/b.json" in builder.parts

    def test_add_directory_with_glob_filter(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.json").write_text("1")
        (src / "b.txt").write_text("x")
        out = tmp_path / "X.pbit"
        builder = PbitArchiveBuilder(output_path=out)
        builder.add_directory(src, include_globs=["*.json"])
        assert "a.json" in builder.parts
        assert "b.txt" not in builder.parts


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


class TestGlobMatching:
    @pytest.mark.parametrize(
        "path,glob,expected",
        [
            ("a.json", "*.json", True),
            ("a.json", "*.txt", False),
            ("sub/a.json", "*.json", False),  # * doesn't match /
            ("sub/a.json", "**/*.json", True),
            ("a.j", "a.?", True),
            ("a.json", "a.?", False),  # ? matches one char; a.json has 5
            ("a.json", "a.????", True),
            ("deep/sub/a.json", "**/*.json", True),
            ("a.json", "a.[jt]son", False),  # bracket not supported
        ],
    )
    def test_match_glob(self, path: str, glob: str, expected: bool) -> None:
        assert _match_glob(path, glob) == expected


# ---------------------------------------------------------------------------
# Component directory discovery
# ---------------------------------------------------------------------------


class TestFindComponentDir:
    def test_finds_semantic_model(self, tmp_path: Path) -> None:
        pbip_dir = tmp_path
        (pbip_dir / "Proj.SemanticModel").mkdir()
        (pbip_dir / "Proj.Report").mkdir()
        (pbip_dir / "Proj.pbip").touch()
        result = _find_component_dir(pbip_dir, (".SemanticModel", ".Dataset"))
        assert result is not None
        assert result.name == "Proj.SemanticModel"

    def test_falls_back_to_dataset_suffix(self, tmp_path: Path) -> None:
        pbip_dir = tmp_path
        (pbip_dir / "Proj.Dataset").mkdir()
        result = _find_component_dir(pbip_dir, (".SemanticModel", ".Dataset"))
        assert result is not None
        assert result.name == "Proj.Dataset"

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        pbip_dir = tmp_path
        (pbip_dir / "Proj.Report").mkdir()
        result = _find_component_dir(pbip_dir, (".SemanticModel", ".Dataset"))
        assert result is None


# ---------------------------------------------------------------------------
# Report layout reconstruction
# ---------------------------------------------------------------------------


class TestBuildReportLayoutFromPbip:
    def test_collapses_pbir_into_layout(self, tmp_path: Path) -> None:
        report_dir = tmp_path / "Proj.Report"
        definition_dir = report_dir / "definition"
        pages_dir = definition_dir / "pages" / "Main" / "visuals"
        pages_dir.mkdir(parents=True)
        (report_dir / "definition.pbir").write_text("{}")

        (definition_dir / "report.json").write_text(
            json.dumps(
                {
                    "pages": [{"name": "Main", "displayName": "Main"}],
                    "settings": {"displayOption": "fitToPage"},
                }
            )
        )
        (definition_dir / "pages" / "Main" / "page.json").write_text(
            json.dumps(
                {
                    "$schema": "http://powerbi.com/product/schema#page",
                    "pageSize": {"width": 1280, "height": 720},
                }
            )
        )
        (pages_dir / "visual_a.json").write_text(
            json.dumps({"name": "visual_a", "visualType": "card"})
        )
        (pages_dir / "visual_b.json").write_text(
            json.dumps({"name": "visual_b", "visualType": "barChart"})
        )

        layout = _build_report_layout_from_pbip(report_dir)
        assert layout["settings"]["displayOption"] == "fitToPage"
        assert len(layout["pages"]) == 1
        page = layout["pages"][0]
        assert page["name"] == "Main"
        assert page["pageSize"]["width"] == 1280
        assert len(page["visualContainers"]) == 2
        names = {v["name"] for v in page["visualContainers"]}
        assert names == {"visual_a", "visual_b"}

    def test_empty_folder_returns_default_layout(self, tmp_path: Path) -> None:
        report_dir = tmp_path / "Proj.Report"
        report_dir.mkdir()
        layout = _build_report_layout_from_pbip(report_dir)
        assert layout == {"pages": [], "settings": {}}


# ---------------------------------------------------------------------------
# Project name extraction
# ---------------------------------------------------------------------------


class TestExtractProjectName:
    def test_reads_from_pbip_opener(self, tmp_path: Path) -> None:
        pbip_dir = tmp_path
        (pbip_dir / "MyProject.pbip").write_text(
            json.dumps({"name": "MyProject", "version": "1.0"})
        )
        assert _extract_project_name(pbip_dir) == "MyProject"

    def test_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        # No .pbip opener; fall back to the directory's own name.
        pbip_dir = tmp_path / "FallbackName"
        pbip_dir.mkdir()
        assert _extract_project_name(pbip_dir) == "FallbackName"

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        pbip_dir = tmp_path
        (pbip_dir / "Broken.pbip").write_text("not json")
        assert _extract_project_name(pbip_dir) == pbip_dir.name


# ---------------------------------------------------------------------------
# End-to-end export
# ---------------------------------------------------------------------------


class TestEndToEndExport:
    def _make_pbip(self, tmp_path: Path) -> Path:
        pbip_dir = tmp_path / "TestProj.pbipdir"
        sm = pbip_dir / "TestProj.SemanticModel" / "definition" / "tables"
        sr = pbip_dir / "TestProj.Report" / "definition" / "pages" / "Main" / "visuals"
        sm.mkdir(parents=True)
        sr.mkdir(parents=True)

        (pbip_dir / "TestProj.pbip").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "name": "TestProj",
                    "components": [
                        {"type": "semanticModel", "path": "TestProj.SemanticModel"},
                        {"type": "report", "path": "TestProj.Report"},
                    ],
                }
            )
        )
        (pbip_dir / "TestProj.SemanticModel" / "definition.pbism").write_text("{}")
        (pbip_dir / "TestProj.Report" / "definition.pbir").write_text("{}")
        (pbip_dir / "TestProj.SemanticModel" / "definition" / "model.tmdl").write_text(
            "table 'X' { column 'id' { dataType: int64 } }"
        )
        (
            pbip_dir / "TestProj.SemanticModel" / "definition" / "relationships.tmdl"
        ).write_text("")
        (pbip_dir / "TestProj.Report" / "definition" / "report.json").write_text(
            json.dumps({"pages": [{"name": "Main"}], "settings": {}})
        )
        return pbip_dir

    def test_export_as_pbit_zip_produces_opc_archive(self, tmp_path: Path) -> None:
        pbip_dir = self._make_pbip(tmp_path)
        exporter = PBIPExporter()
        output = pbip_dir / "TestProj.pbit"
        result = exporter.export_as_pbit_zip(str(pbip_dir), str(output))
        assert Path(result).exists()
        # Round-trip: open the archive and verify the canonical parts.
        with zipfile.ZipFile(result) as z:
            names = set(z.namelist())
            for required in (
                "[Content_Types].xml",
                "Version",
                "Metadata",
                "Settings",
                "SecurityBindings",
                "DiagramLayout",
                "DataModelSchema",
                "DataMashup",
                "Report/Layout",
            ):
                assert required in names
            # Content_Types has BOM.
            assert z.read("[Content_Types].xml").startswith(b"\xef\xbb\xbf")

    def test_export_metadata_carries_template_name(self, tmp_path: Path) -> None:
        pbip_dir = self._make_pbip(tmp_path)
        exporter = PBIPExporter()
        output = exporter.export_as_pbit_zip(
            str(pbip_dir), str(pbip_dir / "TestProj.pbit")
        )
        with zipfile.ZipFile(output) as z:
            meta = json.loads(z.read("Metadata").decode("utf-8"))
            assert meta["title"] == "TestProj"
            # Generation timestamp is set.
            assert meta["created"]
            assert meta["generator"] == "nl2pbip"

    def test_build_pbit_archive_is_alias_for_export(self, tmp_path: Path) -> None:
        pbip_dir = self._make_pbip(tmp_path)
        exporter = PBIPExporter()
        result = exporter.build_pbit_archive(str(pbip_dir), str(pbip_dir / "out.pbit"))
        assert Path(result).exists()

    def test_export_fails_cleanly_on_missing_pbip(self, tmp_path: Path) -> None:
        exporter = PBIPExporter()
        with pytest.raises(FileNotFoundError):
            exporter.export_as_pbit_zip(
                str(tmp_path / "does-not-exist"), str(tmp_path / "out.pbit")
            )
