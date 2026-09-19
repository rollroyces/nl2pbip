"""Tests for the nl2pbip MCP server.

Mirrors the existing orchestrator test style: deterministic
``_StaticLLM`` stub + ``tmp_path`` fixtures + black-box assertions
on the four MCP tool functions.

The MCP framework wraps each tool handler with its own argument
parsing, but the underlying functions in
:mod:`nl2pbip.mcp_server.server` are plain Python and can be
exercised directly. We test the handlers (not the JSON-RPC
framing) because the framework is upstream and its own test suite
covers the wire format.

The whole module is gated on the optional ``[mcp]`` extra via
:func:`pytest.importorskip` so CI's default ``.[dev]`` install
(which doesn't pull the MCP SDK) skips this file cleanly
instead of erroring at collection time. Run locally with
``pip install -e ".[dev,mcp]"`` to exercise the full suite.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("mcp", reason="MCP server tests require the [mcp] extra")

from nl2pbip.mcp_server import build_server
from nl2pbip.mcp_server.server import (
    Nl2PbipMcpServer,
    _tool_generate_report,
    _tool_inspect_dataset,
    _tool_validate_pbip,
    _tool_version,
)
from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY


class _StaticLLM:
    """Deterministic LLM stub. Returns ``plan`` on every call.

    Identical shape to the one in :mod:`tests.test_orchestrator` so
    the same fixture plans work in both places.
    """

    def __init__(self, plan: List[Dict[str, Any]]) -> None:
        self._plan = plan
        self.calls = 0

    def generate(self, _: List[Dict[str, str]]) -> str:  # type: ignore[override]
        self.calls += 1
        return json.dumps({"plan": self._plan})


def _minimal_package_plan(tmp_path: Path) -> List[Dict[str, Any]]:
    """A 4-step plan that creates a table + page + measure + visual + packages."""

    return [
        {"tool": "add_report_page", "args": {"page": "Main", "display_name": "Test"}},
        {
            "tool": "create_table",
            "args": {
                "table_name": "Sales",
                "columns": [{"name": "Amount", "data_type": "decimal"}],
            },
        },
        {
            "tool": "add_measure",
            "args": {
                "table_name": "Sales",
                "measure_name": "Revenue",
                "expression": "SUM(Sales[Amount])",
            },
        },
        {
            "tool": "add_visual",
            "args": {
                "page": "Main",
                "visual_type": "card",
                "bindings": {"Values": ["[Revenue]"]},
            },
        },
        {
            "tool": "package_pbip",
            "args": {
                "output_path": str(tmp_path / "MCPGenerated.pbipdir"),
                "project_name": "MCPGenerated",
                "overwrite": True,
            },
        },
    ]


# ---------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------


def _list_tool_names(server: Any) -> set[str]:
    """Resolve a FastMCP server's registered tool names.

    ``await server.list_tools()`` is the public introspection API
    in mcp 1.x. Returns a list of ``Tool`` objects with ``name``
    and ``description`` attributes. We wrap it in a helper so a
    future FastMCP API change can be isolated to one place.
    """

    tools = asyncio.run(server.list_tools())
    return {tool.name for tool in tools}


def test_build_server_exposes_four_tools() -> None:
    """The four tools must be present on the FastMCP instance.

    Uses ``list_tools()`` (the FastMCP public introspection API in
    mcp 1.x) and asserts the exact tool names — any drift here is
    a breaking change to the MCP surface.
    """

    server = build_server()
    assert _list_tool_names(server) == {
        "generate_report",
        "validate_pbip",
        "inspect_dataset",
        "version",
    }


def test_nl2pbip_mcp_server_class_wraps_build_server() -> None:
    """The convenience class proxies to ``build_server``."""

    instance = Nl2PbipMcpServer(name="nl2pbip-test")
    assert instance.name == "nl2pbip-test"
    assert isinstance(instance.server, type(build_server()))


def test_build_server_tools_carry_descriptions() -> None:
    """Every tool must have a non-empty description.

    MCP clients render the description in their tool picker; an
    empty description makes the tool effectively invisible.
    """

    server = build_server()
    tools = asyncio.run(server.list_tools())
    for tool in tools:
        assert tool.description, f"tool {tool.name!r} has empty description"


# ---------------------------------------------------------------------
# version() — no I/O, no LLM
# ---------------------------------------------------------------------


def test_version_reports_library_and_prompt_versions() -> None:
    info = _tool_version()
    assert info["server_name"] == "nl2pbip-mcp"
    assert info["transport"] == "stdio"
    # Library version is a non-empty string in semver-ish shape.
    assert isinstance(info["library_version"], str)
    parts = info["library_version"].split(".")
    assert len(parts) >= 2 and all(
        p.isdigit() for p in parts
    ), f"library_version {info['library_version']!r} does not look like semver"
    # Prompt version is the integer constant.
    assert isinstance(info["prompt_version"], int)
    assert info["prompt_version"] >= 7


# ---------------------------------------------------------------------
# inspect_dataset() — no LLM, real file I/O
# ---------------------------------------------------------------------


def test_inspect_dataset_csv_round_trip(tmp_path: Path) -> None:
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text(
        "region,amount\nnorth,100\nsouth,200\nnorth,150\nsouth,300\n",
        encoding="utf-8",
    )
    profile = _tool_inspect_dataset(str(csv_path), max_rows=10)
    assert profile["source_kind"] == "csv"
    assert profile["tables"], "expected one table profile"
    table = profile["tables"][0]
    column_names = {c["name"] for c in table["columns"]}
    assert column_names == {"region", "amount"}
    # Top-N distinct values: ``distinct_examples`` is a flat list of
    # the most-frequent values (no counts in the dataclass shape).
    region_col = next(c for c in table["columns"] if c["name"] == "region")
    distinct_values = list(region_col["distinct_examples"])
    assert "north" in distinct_values
    assert "south" in distinct_values
    # ``north`` and ``south`` each appear twice, so both must be in
    # the top-5 (which is the profiler's hard-coded top_n).
    assert len(distinct_values) == 2


def test_inspect_dataset_redacts_distinct_values(tmp_path: Path) -> None:
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("region,amount\nnorth,100\n", encoding="utf-8")
    profile = _tool_inspect_dataset(str(csv_path), redact_distinct_values=True)
    table = profile["tables"][0]
    for column in table["columns"]:
        # The column profile either drops ``distinct_examples`` entirely
        # or leaves it as an empty list — both signal redaction.
        assert column.get("distinct_examples", []) == []


def test_inspect_dataset_rejects_non_positive_max_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_rows must be positive"):
        _tool_inspect_dataset(str(csv_path), max_rows=0)


# ---------------------------------------------------------------------
# generate_report() — full pipeline via stub LLM
# ---------------------------------------------------------------------


def test_generate_report_packages_pbip_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete stub-LLM plan should produce a real .pbipdir on disk.

    We monkeypatch :class:`StructuredLLMClient` so the orchestrator
    never reaches the network — the MCP tool handler is responsible
    for the pipeline plumbing (workspace, output, catalog wiring),
    not the LLM itself.
    """

    from nl2pbip.mcp_server import server as mcp_server_module

    plan = _minimal_package_plan(tmp_path)

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def generate(self, _: List[Dict[str, str]]) -> str:
            return json.dumps({"plan": plan})

    monkeypatch.setattr(mcp_server_module, "StructuredLLMClient", _StubClient)

    result = _tool_generate_report(
        prompt="Build a tiny revenue card",
        workspace=str(tmp_path / "workspace"),
        output_dir=str(tmp_path / "out"),
        project_name="MCPGenerated",
        max_cost_usd=0.0,  # disable budget guard
    )

    assert result["status"] == "ok"
    project_path = Path(result["project_path"])
    assert project_path.exists()
    assert project_path.is_dir()
    # The PBIP layout convention names the report folder with .Report
    # suffix — discover it and confirm the package produced both halves.
    report_folders = [p for p in project_path.iterdir() if p.name.endswith(".Report")]
    assert report_folders, "expected <name>.Report/ under the PBIP folder"
    assert any(p.name.endswith(".SemanticModel") for p in project_path.iterdir())


def test_generate_report_rejects_empty_prompt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _tool_generate_report(prompt="", workspace=str(tmp_path / "ws"))


# ---------------------------------------------------------------------
# validate_pbip() — round-trip on the real example project
# ---------------------------------------------------------------------


def test_validate_pbip_on_real_example_artifact() -> None:
    """The committed SalesInsights example must validate cleanly.

    ``artifacts/SalesInsights.pbipdir`` is generated by the
    project's own orchestrator (it's the package output of the
    integration test fixtures), so if validate_pbip flags errors
    here it means the validator itself is broken — not the artifact.
    """

    artifact_root = Path(__file__).parent.parent / "artifacts" / "SalesInsights.pbipdir"
    if not artifact_root.exists():
        pytest.skip(
            "SalesInsights.pbipdir not present in this checkout — "
            "run the orchestrator integration test first to materialise it."
        )

    result = _tool_validate_pbip(str(artifact_root))
    assert (
        result["status"] == "ok"
    ), f"validator reported issues on the committed fixture: {result['issues']}"
    assert result["pages_validated"] >= 1
    assert result["visuals_validated"] >= 1
    assert result["errors"] == 0
    assert result["warnings"] == 0


def test_validate_pbip_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        _tool_validate_pbip(str(tmp_path / "does-not-exist"))


def test_validate_pbip_rejects_missing_report_folder(tmp_path: Path) -> None:
    empty_pbip = tmp_path / "Empty.pbipdir"
    empty_pbip.mkdir()
    with pytest.raises(FileNotFoundError, match=r"No \.Report folder"):
        _tool_validate_pbip(str(empty_pbip))


# ---------------------------------------------------------------------
# Python-API smoke (not over the JSON-RPC wire)
# ---------------------------------------------------------------------


def test_orchestrator_stub_invocation_still_works(tmp_path: Path) -> None:
    """Lock-in: monkeypatching the LLM client from a test must not
    regress the orchestrator's contract — proves the MCP handler
    continues to delegate to the same primitives the rest of the
    project uses.
    """

    plan = [
        {"tool": "add_report_page", "args": {"page": "P", "display_name": "P"}},
        {
            "tool": "create_table",
            "args": {
                "table_name": "T",
                "columns": [{"name": "C", "data_type": "int64"}],
            },
        },
    ]

    orchestrator = Orchestrator(llm_client=_StaticLLM(plan))  # type: ignore[arg-type]
    register_builtin_tools(orchestrator)
    results = orchestrator.run(
        "x",
        context={
            MODEL_PATH_KEY: str(tmp_path / "m.tmdl"),
            REPORT_PATH_KEY: str(tmp_path / "r.json"),
            "dax_catalog_path": str(
                Path(__file__).parent.parent / "nl2pbip" / "dax_library.json"
            ),
        },
    )
    assert [r.tool for r in results] == ["add_report_page", "create_table"]
