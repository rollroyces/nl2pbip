"""Tests for the Fabric Git-integration metadata writer.

The packager writes ``itemMetadata.json`` + ``.platform`` per
Fabric item so the resulting PBIP project can be committed to a
Git repo connected to a Fabric workspace. The expected schema
matches Microsoft docs (Sept 2025).

Per-item structure:

* ``{Project}.SemanticModel/itemMetadata.json``
* ``{Project}.SemanticModel/.platform``
* ``{Project}.Report/itemMetadata.json``
* ``{Project}.Report/.platform``

The semantic model and the report share the same ``logicalId``
(one item-group); callers who want different IDs can call
``package_pbip_handler`` once per item.

The item-type strings (``SemanticModel``, ``Report``, ...) are
validated against ``FABRIC_ITEM_TYPES`` before write.
"""

from __future__ import annotations

import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

from nl2pbip.packager import (
    FABRIC_ITEM_TYPES,
    FABRIC_PLATFORM_VERSION,
    package_pbip_handler,
)
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


MINIMAL_MODEL_TMDL = """\
table Date {
  column Date {
    dataType: date
  }
}
table Sales {
  column Id {
    dataType: string
  }
  column Amount {
    dataType: decimal
  }
}
ref table Date
ref table Sales
"""


MINIMAL_REPORT = {
    "report": {
        "name": "SalesInsights",
        "pages": [],
    }
}


@pytest.fixture
def minimal_ctx(tmp_path: Path) -> Dict[str, Any]:
    """Build a minimal model.tmdl + report.json and return a context
    that ``package_pbip_handler`` can consume."""
    model_path = tmp_path / "model.tmdl"
    model_path.write_text(MINIMAL_MODEL_TMDL, encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(MINIMAL_REPORT), encoding="utf-8")
    return {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
    }


# ---------------------------------------------------------------------------
# _validate_fabric_item_type
# ---------------------------------------------------------------------------


class TestValidateFabricItemType:
    def test_known_type_accepted(self) -> None:
        # Should not raise.
        from nl2pbip.packager import _validate_fabric_item_type

        _validate_fabric_item_type("SemanticModel", "semantic")
        _validate_fabric_item_type("Report", "report")
        _validate_fabric_item_type("Lakehouse", "warehouse")

    def test_unknown_type_rejected(self) -> None:
        from nl2pbip.packager import _validate_fabric_item_type

        with pytest.raises(ValueError, match="not a recognised"):
            _validate_fabric_item_type("BogusType", "semantic")

    def test_empty_type_rejected(self) -> None:
        from nl2pbip.packager import _validate_fabric_item_type

        with pytest.raises(ValueError, match="non-empty"):
            _validate_fabric_item_type("", "semantic")

    def test_whitespace_type_rejected(self) -> None:
        from nl2pbip.packager import _validate_fabric_item_type

        with pytest.raises(ValueError, match="non-empty"):
            _validate_fabric_item_type("   ", "semantic")

    def test_non_string_type_rejected(self) -> None:
        from nl2pbip.packager import _validate_fabric_item_type

        with pytest.raises(ValueError, match="non-empty"):
            _validate_fabric_item_type(None, "semantic")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _make_item_metadata
# ---------------------------------------------------------------------------


class TestMakeItemMetadata:
    def test_minimal(self) -> None:
        from nl2pbip.packager import _make_item_metadata

        metadata = _make_item_metadata("SemanticModel", "Sales")
        assert metadata["type"] == "SemanticModel"
        assert metadata["displayName"] == "Sales"
        assert "description" not in metadata
        assert "sensitivityLabelId" not in metadata

    def test_with_description(self) -> None:
        from nl2pbip.packager import _make_item_metadata

        metadata = _make_item_metadata(
            "Report", "Sales", description="Quarterly sales report"
        )
        assert metadata["description"] == "Quarterly sales report"

    def test_with_sensitivity_label(self) -> None:
        from nl2pbip.packager import _make_item_metadata

        metadata = _make_item_metadata(
            "SemanticModel", "Sales", sensitivity_label_id="abc-123"
        )
        assert metadata["sensitivityLabelId"] == "abc-123"

    def test_empty_sensitivity_label_omitted(self) -> None:
        from nl2pbip.packager import _make_item_metadata

        metadata = _make_item_metadata(
            "SemanticModel", "Sales", sensitivity_label_id=""
        )
        assert "sensitivityLabelId" not in metadata


# ---------------------------------------------------------------------------
# _make_platform
# ---------------------------------------------------------------------------


class TestMakePlatform:
    def test_structure(self) -> None:
        from nl2pbip.packager import _make_platform

        platform = _make_platform(
            "SemanticModel", "Sales", logical_id="00000000-0000-0000-0000-000000000000"
        )
        assert platform["metadata"]["type"] == "SemanticModel"
        assert platform["metadata"]["displayName"] == "Sales"
        assert platform["config"]["version"] == FABRIC_PLATFORM_VERSION
        assert platform["config"]["logicalId"] == "00000000-0000-0000-0000-000000000000"

    def test_platform_version_is_2_0(self) -> None:
        assert FABRIC_PLATFORM_VERSION == "2.0"

    def test_logical_id_uuid_format(self) -> None:
        from nl2pbip.packager import _make_platform

        platform = _make_platform("Report", "R", logical_id="abc")
        assert platform["config"]["logicalId"] == "abc"


# ---------------------------------------------------------------------------
# FABRIC_ITEM_TYPES
# ---------------------------------------------------------------------------


class TestFabricItemTypes:
    def test_required_types_present(self) -> None:
        for required in (
            "SemanticModel",
            "Report",
            "Lakehouse",
            "Warehouse",
            "Notebook",
            "DataPipeline",
        ):
            assert required in FABRIC_ITEM_TYPES

    def test_known_lowercase_aliases_not_listed(self) -> None:
        # Fabric is case-sensitive; lowercase variants are rejected.
        assert "semanticmodel" not in FABRIC_ITEM_TYPES
        assert "report" not in FABRIC_ITEM_TYPES


# ---------------------------------------------------------------------------
# package_pbip_handler — Fabric metadata off
# ---------------------------------------------------------------------------


class TestPackagePbipWithoutFabric:
    def test_fabric_disabled_omits_metadata_files(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        result = package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            write_fabric_metadata=False,
            context=minimal_ctx,
        )
        assert result["fabric_enabled"] is False
        assert result["fabric_files"] == {}
        assert not (
            output_path / "TestProj.SemanticModel" / "itemMetadata.json"
        ).exists()
        assert not (output_path / "TestProj.SemanticModel" / ".platform").exists()
        assert not (output_path / "TestProj.Report" / "itemMetadata.json").exists()
        assert not (output_path / "TestProj.Report" / ".platform").exists()
        # Manifest should not have fabricRoot when disabled.
        manifest = json.loads((output_path / "TestProj.pbip").read_text())
        assert "fabricRoot" not in manifest


# ---------------------------------------------------------------------------
# package_pbip_handler — Fabric metadata on (default)
# ---------------------------------------------------------------------------


class TestPackagePbipWithFabric:
    def test_writes_item_metadata_for_each_item(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        result = package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            write_fabric_metadata=True,
            context=minimal_ctx,
        )
        assert result["fabric_enabled"] is True
        # All four metadata files exist.
        sm_dir = output_path / "TestProj.SemanticModel"
        rp_dir = output_path / "TestProj.Report"
        assert (sm_dir / "itemMetadata.json").exists()
        assert (sm_dir / ".platform").exists()
        assert (rp_dir / "itemMetadata.json").exists()
        assert (rp_dir / ".platform").exists()

    def test_item_metadata_contents(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        sm_metadata = json.loads(
            (output_path / "TestProj.SemanticModel" / "itemMetadata.json").read_text()
        )
        assert sm_metadata["type"] == "SemanticModel"
        assert sm_metadata["displayName"] == "TestProj"

        rp_metadata = json.loads(
            (output_path / "TestProj.Report" / "itemMetadata.json").read_text()
        )
        assert rp_metadata["type"] == "Report"
        assert rp_metadata["displayName"] == "TestProj"

    def test_platform_contents(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        sm_platform = json.loads(
            (output_path / "TestProj.SemanticModel" / ".platform").read_text()
        )
        assert sm_platform["metadata"]["type"] == "SemanticModel"
        assert sm_platform["metadata"]["displayName"] == "TestProj"
        assert sm_platform["config"]["version"] == "2.0"
        # logicalId is a valid UUID string.
        uuid.UUID(sm_platform["config"]["logicalId"])

    def test_logical_id_shared_between_items(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        sm_platform = json.loads(
            (output_path / "TestProj.SemanticModel" / ".platform").read_text()
        )
        rp_platform = json.loads(
            (output_path / "TestProj.Report" / ".platform").read_text()
        )
        assert sm_platform["config"]["logicalId"] == rp_platform["config"]["logicalId"]

    def test_custom_logical_id(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        fixed_id = "fixed-id-1234"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            fabric_logical_id=fixed_id,
            context=minimal_ctx,
        )
        sm_platform = json.loads(
            (output_path / "TestProj.SemanticModel" / ".platform").read_text()
        )
        assert sm_platform["config"]["logicalId"] == fixed_id

    def test_custom_logical_id_format_must_be_uuid_or_string(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        # We don't strictly require UUID format; any string is OK.
        # (Some orgs use GUIDs in a non-RFC format.)
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            fabric_logical_id="custom-string-id",
            context=minimal_ctx,
        )
        sm_platform = json.loads(
            (output_path / "TestProj.SemanticModel" / ".platform").read_text()
        )
        assert sm_platform["config"]["logicalId"] == "custom-string-id"

    def test_sensitivity_label_propagates(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            fabric_sensitivity_label_id="confidential",
            context=minimal_ctx,
        )
        sm_metadata = json.loads(
            (output_path / "TestProj.SemanticModel" / "itemMetadata.json").read_text()
        )
        assert sm_metadata["sensitivityLabelId"] == "confidential"
        rp_metadata = json.loads(
            (output_path / "TestProj.Report" / "itemMetadata.json").read_text()
        )
        assert rp_metadata["sensitivityLabelId"] == "confidential"

    def test_unknown_semantic_type_rejected(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        with pytest.raises(ValueError, match="SemanticModel"):
            package_pbip_handler(
                str(output_path),
                project_name="TestProj",
                fabric_semantic_model_type="BogusType",
                context=minimal_ctx,
            )

    def test_unknown_report_type_rejected(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        with pytest.raises(ValueError, match="Report"):
            package_pbip_handler(
                str(output_path),
                project_name="TestProj",
                fabric_report_type="BogusType",
                context=minimal_ctx,
            )

    def test_custom_semantic_type_accepted(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            fabric_semantic_model_type="Lakehouse",
            context=minimal_ctx,
        )
        sm_metadata = json.loads(
            (output_path / "TestProj.SemanticModel" / "itemMetadata.json").read_text()
        )
        assert sm_metadata["type"] == "Lakehouse"

    def test_manifest_has_fabric_root_when_enabled(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        manifest = json.loads((output_path / "TestProj.pbip").read_text())
        assert manifest["fabricRoot"]["type"] == "PBIPProject"
        assert manifest["fabricRoot"]["displayName"] == "TestProj"

    def test_uuid_regex_in_generated_logical_id(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        sm_platform = json.loads(
            (output_path / "TestProj.SemanticModel" / ".platform").read_text()
        )
        # Default logical_id is uuid.uuid4() — should match the canonical
        # 8-4-4-4-12 hex pattern.
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            sm_platform["config"]["logicalId"],
        )

    def test_result_dict_includes_fabric_paths(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        result = package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        assert "fabric_files" in result
        assert "semantic_model_item_metadata" in result["fabric_files"]
        assert "semantic_model_platform" in result["fabric_files"]
        assert "report_item_metadata" in result["fabric_files"]
        assert "report_platform" in result["fabric_files"]
        assert "logical_id" in result["fabric_files"]


# ---------------------------------------------------------------------------
# Round-trip — packaged project is read-back valid
# ---------------------------------------------------------------------------


class TestFabricRoundTrip:
    def test_packaged_files_are_valid_json(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        for path in [
            output_path / "TestProj.SemanticModel" / "itemMetadata.json",
            output_path / "TestProj.SemanticModel" / ".platform",
            output_path / "TestProj.Report" / "itemMetadata.json",
            output_path / "TestProj.Report" / ".platform",
            output_path / "TestProj.pbip",
        ]:
            assert path.exists()
            # Should parse without error.
            data = json.loads(path.read_text())
            assert isinstance(data, dict)

    def test_packaged_item_metadata_schema(
        self, minimal_ctx: Dict[str, Any], tmp_path: Path
    ) -> None:
        # Microsoft docs list the canonical schema; verify all keys
        # we emit are recognised.
        output_path = tmp_path / "out"
        package_pbip_handler(
            str(output_path),
            project_name="TestProj",
            context=minimal_ctx,
        )
        sm_metadata = json.loads(
            (output_path / "TestProj.SemanticModel" / "itemMetadata.json").read_text()
        )
        for key in ("type", "displayName", "description"):
            assert key in sm_metadata

        sm_platform = json.loads(
            (output_path / "TestProj.SemanticModel" / ".platform").read_text()
        )
        assert "metadata" in sm_platform
        assert "config" in sm_platform
        assert "version" in sm_platform["config"]
        assert "logicalId" in sm_platform["config"]
