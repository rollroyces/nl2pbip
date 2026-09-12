"""Package nl2pbip intermediate artifacts into a PBIP folder."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import (
    MODEL_PATH_KEY,
    TMDLModel,
    parse_tmdl_text,
    roles_workspace_dir,
)

PBIP_VERSION = "1.0"
ROLES_DEFINITION_PATH = "definition/roles"

# Canonical Fabric item type strings. Fabric accepts the casing shown
# here; other casings are silently downgraded or rejected. Keep this
# list in sync with the Microsoft docs.
FABRIC_ITEM_TYPES = frozenset(
    {
        "SemanticModel",
        "Report",
        "PaginatedReport",
        "Dashboard",
        "Dataflow",
        "Datamart",
        "Lakehouse",
        "Warehouse",
        "Notebook",
        "SparkJobDefinition",
        "Eventstream",
        "KQLDatabase",
        "KQLQueryset",
        "MLModel",
        "MLExperiment",
        "DataPipeline",
        "Environment",
        "SQLEndpoint",
        "MirroredDatabase",
        "Reflex",
        "GraphQLApi",
    }
)
FABRIC_PLATFORM_VERSION = "2.0"


def package_pbip_handler(
    output_path: str,
    project_name: str | None = None,
    overwrite: bool = False,
    write_fabric_metadata: bool = True,
    fabric_semantic_model_type: str = "SemanticModel",
    fabric_report_type: str = "Report",
    fabric_logical_id: Optional[str] = None,
    fabric_sensitivity_label_id: Optional[str] = None,
    context: Dict[str, Any] | None = None,
    **_: Any,
) -> Dict[str, Any]:
    project_dir = Path(output_path).expanduser()
    project_name = project_name or project_dir.name or "NL2PBIP"
    if write_fabric_metadata:
        _validate_fabric_item_type(fabric_semantic_model_type, "semantic")
        _validate_fabric_item_type(fabric_report_type, "report")
    model = _load_model(context)
    roles = _load_roles(context)
    report_payload = _load_report(context)

    _prepare_output_dir(project_dir, overwrite)
    semantic_paths = _write_semantic_model(project_dir, project_name, model, roles)
    report_paths = _write_report_files(project_dir, project_name, report_payload)
    project_manifest = _write_project_manifests(
        project_dir, project_name, write_fabric_metadata
    )
    fabric_paths: Dict[str, Any] = {}
    if write_fabric_metadata:
        logical_id = fabric_logical_id or str(uuid.uuid4())
        fabric_paths = _write_fabric_metadata(
            project_dir,
            project_name,
            logical_id=logical_id,
            semantic_model_type=fabric_semantic_model_type,
            report_type=fabric_report_type,
            sensitivity_label_id=fabric_sensitivity_label_id,
        )

    return {
        "status": "success",
        "project": project_name,
        "project_path": str(project_dir),
        "project_manifest": project_manifest,
        "semantic_files": semantic_paths,
        "report_files": report_paths,
        "fabric_files": fabric_paths,
        "fabric_enabled": write_fabric_metadata,
    }


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------
def _load_model(context: Dict[str, Any] | None) -> TMDLModel:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("Packaging requires 'model_path' within context.")
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    if not model_path.exists():
        raise FileNotFoundError(f"Model file not found at {model_path}.")
    model_text = model_path.read_text(encoding="utf-8")
    return parse_tmdl_text(model_text)


def _load_roles(context: Dict[str, Any] | None) -> Dict[str, str]:
    if not context or MODEL_PATH_KEY not in context:
        raise ValueError("Packaging requires 'model_path' within context.")
    model_path = Path(context[MODEL_PATH_KEY]).expanduser()
    roles_dir = roles_workspace_dir(model_path)
    roles: Dict[str, str] = {}
    if roles_dir.exists():
        for role_file in roles_dir.glob("*.tmdl"):
            roles[role_file.name] = role_file.read_text(encoding="utf-8")
    return roles


def _load_report(context: Dict[str, Any] | None) -> Dict[str, Any]:
    if not context or REPORT_PATH_KEY not in context:
        raise ValueError("Packaging requires 'report_path' within context.")
    report_path = Path(context[REPORT_PATH_KEY]).expanduser()
    if not report_path.exists():
        raise FileNotFoundError(f"Report file not found at {report_path}.")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "pbir" in payload:
        return payload["pbir"]
    return payload


# ---------------------------------------------------------------------------
# Directory + manifest generation
# ---------------------------------------------------------------------------
def _prepare_output_dir(project_dir: Path, overwrite: bool) -> None:
    if project_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"PBIP output directory {project_dir} already exists. Set overwrite=True to replace."
            )
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)


def _write_project_manifests(
    project_dir: Path,
    project_name: str,
    write_fabric_metadata: bool = True,
) -> Dict[str, str]:
    manifest = {
        "version": PBIP_VERSION,
        "name": project_name,
        "components": [
            {"type": "semanticModel", "path": f"{project_name}.SemanticModel"},
            {"type": "report", "path": f"{project_name}.Report"},
        ],
    }
    # When fabric metadata is on, the project root is itself a Fabric
    # item (``PBIPProject``). Otherwise it stays as a plain folder.
    if write_fabric_metadata:
        manifest["fabricRoot"] = {
            "type": "PBIPProject",
            "displayName": project_name,
        }
    manifest_path = project_dir / f"{project_name}.pbip"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"pbip": str(manifest_path)}


# ---------------------------------------------------------------------------
# Fabric Git integration metadata
# ---------------------------------------------------------------------------


def _validate_fabric_item_type(item_type: str, label: str) -> None:
    """Raise ``ValueError`` if ``item_type`` is not a known Fabric type."""
    if not isinstance(item_type, str) or not item_type.strip():
        raise ValueError(f"fabric_{label}_type must be a non-empty string.")
    if item_type not in FABRIC_ITEM_TYPES:
        raise ValueError(
            f"fabric_{label}_type '{item_type}' is not a recognised "
            f"Fabric item type. Valid: {sorted(FABRIC_ITEM_TYPES)}."
        )


def _make_item_metadata(
    item_type: str,
    display_name: str,
    description: str = "",
    sensitivity_label_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``itemMetadata.json`` payload for a Fabric item."""
    metadata: Dict[str, Any] = {
        "type": item_type,
        "displayName": display_name,
    }
    if description:
        metadata["description"] = description
    if sensitivity_label_id:
        metadata["sensitivityLabelId"] = sensitivity_label_id
    return metadata


def _make_platform(
    item_type: str,
    display_name: str,
    logical_id: str,
) -> Dict[str, Any]:
    """Build the ``.platform`` payload for a Fabric item."""
    return {
        "metadata": {
            "type": item_type,
            "displayName": display_name,
        },
        "config": {
            "version": FABRIC_PLATFORM_VERSION,
            "logicalId": logical_id,
        },
    }


def _write_fabric_metadata(
    project_dir: Path,
    project_name: str,
    *,
    logical_id: str,
    semantic_model_type: str,
    report_type: str,
    sensitivity_label_id: Optional[str],
) -> Dict[str, str]:
    """Write ``itemMetadata.json`` + ``.platform`` for each Fabric item.

    The function is called once per ``package_pbip`` invocation and
    writes:

    * ``{Project}.SemanticModel/itemMetadata.json``
    * ``{Project}.SemanticModel/.platform``
    * ``{Project}.Report/itemMetadata.json``
    * ``{Project}.Report/.platform``

    The semantic model and the report share the same ``logical_id``;
    Fabric treats them as one item-group. Callers who want different
    IDs can call :func:`package_pbip_handler` once per item.
    """
    paths: Dict[str, str] = {}
    semantic_dir = project_dir / f"{project_name}.SemanticModel"
    report_dir = project_dir / f"{project_name}.Report"

    semantic_metadata = _make_item_metadata(
        semantic_model_type,
        display_name=project_name,
        description=f"Semantic model for {project_name}",
        sensitivity_label_id=sensitivity_label_id,
    )
    semantic_platform = _make_platform(
        semantic_model_type,
        display_name=project_name,
        logical_id=logical_id,
    )
    sm_metadata_path = semantic_dir / "itemMetadata.json"
    sm_platform_path = semantic_dir / ".platform"
    sm_metadata_path.write_text(
        json.dumps(semantic_metadata, indent=2), encoding="utf-8"
    )
    sm_platform_path.write_text(
        json.dumps(semantic_platform, indent=2), encoding="utf-8"
    )
    paths["semantic_model_item_metadata"] = str(sm_metadata_path)
    paths["semantic_model_platform"] = str(sm_platform_path)

    report_metadata = _make_item_metadata(
        report_type,
        display_name=project_name,
        description=f"Report for {project_name}",
        sensitivity_label_id=sensitivity_label_id,
    )
    report_platform = _make_platform(
        report_type,
        display_name=project_name,
        logical_id=logical_id,
    )
    r_metadata_path = report_dir / "itemMetadata.json"
    r_platform_path = report_dir / ".platform"
    r_metadata_path.write_text(json.dumps(report_metadata, indent=2), encoding="utf-8")
    r_platform_path.write_text(json.dumps(report_platform, indent=2), encoding="utf-8")
    paths["report_item_metadata"] = str(r_metadata_path)
    paths["report_platform"] = str(r_platform_path)
    paths["logical_id"] = logical_id
    return paths


# ---------------------------------------------------------------------------
# Semantic model writers
# ---------------------------------------------------------------------------
def _write_semantic_model(
    project_dir: Path,
    project_name: str,
    model: TMDLModel,
    roles: Dict[str, str],
) -> Dict[str, Any]:
    semantic_dir = project_dir / f"{project_name}.SemanticModel"
    definition_dir = semantic_dir / "definition"
    tables_dir = definition_dir / "tables"
    definition_dir.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)

    table_files: Dict[str, str] = {}
    for table in model.tables.values():
        file_name = _safe_name(table.name) + ".tmdl"
        table_path = tables_dir / file_name
        table_path.write_text(table.to_tmdl() + "\n", encoding="utf-8")
        table_files[table.name] = str(table_path)

    model_path = definition_dir / "model.tmdl"
    model_path.write_text(_render_tables_block(model), encoding="utf-8")

    relationships_path = definition_dir / "relationships.tmdl"
    relationships_path.write_text(_render_relationships_block(model), encoding="utf-8")

    roles_dir = _ensure_roles_directory(definition_dir)
    role_paths: Dict[str, str] = {}
    for role_file, content in roles.items():
        target = roles_dir / role_file
        target.write_text(content, encoding="utf-8")
        role_paths[role_file] = str(target)

    definition_pbism = {
        "version": PBIP_VERSION,
        "name": project_name,
        "modelDefinition": "definition/model.tmdl",
        "relationshipsDefinition": "definition/relationships.tmdl",
        "rolesDirectory": ROLES_DEFINITION_PATH,
    }
    (semantic_dir / "definition.pbism").write_text(
        json.dumps(definition_pbism, indent=2), encoding="utf-8"
    )

    return {
        "model": str(model_path),
        "relationships": str(relationships_path),
        "tables": table_files,
        "roles": role_paths,
    }


def _render_tables_block(model: TMDLModel) -> str:
    sections = [table.to_tmdl() for table in model.tables.values()]
    return "\n\n".join(sections).strip() + ("\n" if sections else "")


def _render_relationships_block(model: TMDLModel) -> str:
    if not model.relationships:
        return ""
    return "\n\n".join(rel.to_tmdl() for rel in model.relationships) + "\n"


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------
def _write_report_files(
    project_dir: Path,
    project_name: str,
    report_payload: Dict[str, Any],
) -> Dict[str, Any]:
    report_dir = project_dir / f"{project_name}.Report"
    definition_dir = report_dir / "definition"
    pages_dir = definition_dir / "pages"
    definition_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    report_json_path = definition_dir / "report.json"
    report_json_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")

    recorded_pages: Dict[str, Dict[str, Any]] = {}
    for page in report_payload.get("pages", []):
        page_id = page.get("name") or f"page_{len(recorded_pages) + 1}"
        page_dir = pages_dir / page_id
        visuals_dir = page_dir / "visuals"
        visuals_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "page.json").write_text(
            json.dumps(page, indent=2), encoding="utf-8"
        )
        visuals_map: Dict[str, str] = {}
        for visual in page.get("visualContainers", []):
            visual_name = visual.get("name") or f"visual_{len(visuals_map) + 1}"
            visual_path = visuals_dir / f"{visual_name}.json"
            visual_path.write_text(json.dumps(visual, indent=2), encoding="utf-8")
            visuals_map[visual_name] = str(visual_path)
        recorded_pages[page_id] = {
            "page": str(page_dir / "page.json"),
            "visuals": visuals_map,
        }

    definition_pbir = {
        "version": PBIP_VERSION,
        "name": project_name,
        "reportDefinition": "definition/report.json",
    }
    (report_dir / "definition.pbir").write_text(
        json.dumps(definition_pbir, indent=2), encoding="utf-8"
    )

    return {"report": str(report_json_path), "pages": recorded_pages}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    return "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in name
    )


def _ensure_roles_directory(definition_dir: Path) -> Path:
    roles_dir = definition_dir / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    return roles_dir
