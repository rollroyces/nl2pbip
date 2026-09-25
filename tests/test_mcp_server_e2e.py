"""End-to-end exercise of the MCP server through the real FastMCP wire.

The unit tests in ``test_mcp_server.py`` call the tool handlers
directly (``_tool_validate_pbip(...)`` etc.). Those are fast and
deterministic but they bypass the JSON-RPC framing that real MCP
clients hit. This module goes the other way: it builds a real
``FastMCP`` server, lists the tools through ``server.list_tools()``
(the path every MCP client walks to discover what's available),
and invokes each tool through ``server.call_tool()`` (the JSON-RPC
entry point).

The net catches a different class of regression than the unit
tests:
- FastMCP API drift (e.g. ``call_tool`` return shape changes)
- Tool-description drift (clients render the description in their
  tool picker; an empty description makes the tool effectively
  invisible)
- Round-trip behaviour that only manifests through the actual
  serialise -> dispatch -> call -> serialise -> return path

The whole module is gated on the optional ``[mcp]`` extra via
:func:`pytest.importorskip` so CI's default ``.[dev]`` install
(which doesn't pull the MCP SDK) skips this file cleanly. Run
locally with ``pip install -e ".[dev,mcp]"`` to exercise the
full suite.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("mcp", reason="MCP e2e tests require the [mcp] extra")

from nl2pbip.mcp_server import Nl2PbipMcpServer, build_server


def _call(server: Any, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a FastMCP tool and return the parsed JSON payload.

    FastMCP's ``call_tool`` returns a 2-tuple ``(content_list,
    meta)`` where ``content_list[0]`` is a ``TextContent`` object
    carrying the serialised tool output. All four nl2pbip tools
    return JSON dicts, so we deserialise once here.
    """

    content_list, _meta = asyncio.run(server.call_tool(name, args))
    return json.loads(content_list[0].text)


# ---------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------


def test_build_server_exposes_four_tools_via_wire() -> None:
    """The four tools must be discoverable through
    ``server.list_tools()`` (the FastMCP entry point that every
    MCP client walks at connection time). Any drift in the tool
    names is a breaking change to the MCP surface.
    """

    server = build_server()
    tools = asyncio.run(server.list_tools())
    tool_names = sorted(t.name for t in tools)
    assert tool_names == [
        "generate_report",
        "inspect_dataset",
        "validate_pbip",
        "version",
    ]


def test_tool_descriptions_are_non_empty_on_wire() -> None:
    """MCP clients render the tool description in their tool
    picker; an empty description makes the tool effectively
    invisible. Re-assert here so a future ``server.tool()`` call
    without a description arg fails fast in CI.
    """

    server = build_server()
    tools = asyncio.run(server.list_tools())
    for tool in tools:
        assert tool.description, f"tool {tool.name!r} has empty description"
        assert (
            tool.description.strip()
        ), f"tool {tool.name!r} has whitespace-only description"


def test_two_servers_are_independent() -> None:
    """Two ``build_server()`` calls must produce independent
    instances (no shared mutable state across processes). If
    a future change accidentally caches the FastMCP server at
    module level, this test would surface it via the second
    server's version mismatch or tool-list drift.
    """

    server_a = build_server(name="nl2pbip-a")
    server_b = build_server(name="nl2pbip-b")
    tools_a = asyncio.run(server_a.list_tools())
    tools_b = asyncio.run(server_b.list_tools())
    assert {t.name for t in tools_a} == {t.name for t in tools_b}
    assert len(tools_a) == 4


def test_nl2pbip_mcp_server_wrapper_exposes_same_surface() -> None:
    """The convenience class must surface the same 4 tools."""

    instance = Nl2PbipMcpServer(name="nl2pbip-test")
    tools = asyncio.run(instance.server.list_tools())
    assert len(tools) == 4


# ---------------------------------------------------------------------
# version - no I/O
# ---------------------------------------------------------------------


def test_version_invocation_via_wire() -> None:
    """``version`` must return the expected library + prompt
    versions through the FastMCP return path. The wire-shape
    check is the value: the underlying handler is exercised
    in test_mcp_server.py; this test catches FastMCP serialisation
    regressions."""

    server = build_server()
    payload = _call(server, "version", {})
    assert payload["server_name"] == "nl2pbip-mcp"
    assert payload["transport"] == "stdio"
    assert payload["library_version"]  # non-empty semver-ish string
    assert isinstance(payload["prompt_version"], int)
    assert payload["prompt_version"] >= 7
    assert "python" in payload
    assert isinstance(payload["pid"], int)


# ---------------------------------------------------------------------
# inspect_dataset - CSV round-trip + redaction + bad input
# ---------------------------------------------------------------------


def test_inspect_dataset_csv_round_trip_via_wire(tmp_path: Path) -> None:
    """CSV profile comes back through the wire format with
    the expected column + distinct-value structure.
    """

    csv = tmp_path / "sales.csv"
    csv.write_text(
        "region,amount\nnorth,100\nsouth,200\nnorth,150\nsouth,300\n",
        encoding="utf-8",
    )
    server = build_server()
    payload = _call(
        server,
        "inspect_dataset",
        {
            "source": str(csv),
            "max_rows": 10,
        },
    )
    assert payload["source_kind"] == "csv"
    assert len(payload["tables"]) == 1
    cols = [c["name"] for c in payload["tables"][0]["columns"]]
    assert set(cols) == {"region", "amount"}


def test_inspect_dataset_redact_via_wire(tmp_path: Path) -> None:
    """``redact_distinct_values=True`` strips distinct examples."""

    csv = tmp_path / "sales.csv"
    csv.write_text("region,amount\nnorth,100\n", encoding="utf-8")
    server = build_server()
    payload = _call(
        server,
        "inspect_dataset",
        {
            "source": str(csv),
            "redact_distinct_values": True,
        },
    )
    for column in payload["tables"][0]["columns"]:
        assert column.get("distinct_examples", []) == []


def test_inspect_dataset_rejects_non_positive_max_rows_via_wire(
    tmp_path: Path,
) -> None:
    """``max_rows=0`` must raise a clear, tool-level error — not
    silently return an empty profile or OOM. FastMCP wraps the
    underlying ``ValueError`` in a ``ToolError`` carrying the
    message; assert the message survives the wrap."""

    csv = tmp_path / "empty.csv"
    csv.write_text("a,b\n", encoding="utf-8")
    server = build_server()
    with pytest.raises(Exception) as exc_info:
        _call(
            server,
            "inspect_dataset",
            {
                "source": str(csv),
                "max_rows": 0,
            },
        )
    # FastMCP raises ``ToolError`` wrapping the underlying message.
    # We don't pin the exception class (it's an MCP-internal type)
    # but the underlying "max_rows must be positive" text must be
    # visible to the client.
    assert "max_rows must be positive" in str(exc_info.value)


# ---------------------------------------------------------------------
# validate_pbip - real fixture + error paths
# ---------------------------------------------------------------------


def test_validate_pbip_on_real_example_via_wire() -> None:
    """Round-trip the committed ``artifacts/SalesInsights.pbipdir``
    fixture through the actual FastMCP dispatch. A passing run
    here means the MCP server's wire encoding doesn't drop any
    fields on the way out."""

    fixture_root = Path(__file__).parent.parent / "artifacts" / "SalesInsights.pbipdir"
    if not fixture_root.exists():
        pytest.skip(
            "SalesInsights.pbipdir not present — run the orchestrator "
            "integration test first to materialise it."
        )
    server = build_server()
    payload = _call(server, "validate_pbip", {"pbip_path": str(fixture_root)})
    assert (
        payload["status"] == "ok"
    ), f"validator reported issues on the committed fixture: {payload['issues']}"
    assert payload["pages_validated"] >= 1
    assert payload["visuals_validated"] >= 1
    assert payload["errors"] == 0


def test_validate_pbip_rejects_missing_path_via_wire(tmp_path: Path) -> None:
    """Missing path must raise a clean tool-level error carrying
    the underlying ``FileNotFoundError`` text."""

    server = build_server()
    with pytest.raises(Exception) as exc_info:
        _call(
            server,
            "validate_pbip",
            {
                "pbip_path": str(tmp_path / "does-not-exist"),
            },
        )
    assert "not found" in str(exc_info.value).lower()


def test_validate_pbip_rejects_folder_without_report_via_wire(
    tmp_path: Path,
) -> None:
    """A directory without a ``.Report`` folder must surface a
    clear ``FileNotFoundError`` to the client."""

    empty_pbip = tmp_path / "Empty.pbipdir"
    empty_pbip.mkdir()
    server = build_server()
    with pytest.raises(Exception) as exc_info:
        _call(server, "validate_pbip", {"pbip_path": str(empty_pbip)})
    assert "No .Report folder" in str(exc_info.value)


# ---------------------------------------------------------------------
# generate_report - end-to-end packaging via stub LLM
# ---------------------------------------------------------------------


def test_generate_report_packages_pbip_via_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full NL -> .pbipdir pipeline through the actual FastMCP
    wire format. The stub LLM avoids any network call; the
    dispatcher walks the real JSON-RPC return path."""

    from nl2pbip.mcp_server import server as mcp_module

    class _StubClient:
        provider = "stub"
        model = "stub-model"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def generate(self, _: List[Dict[str, str]]) -> str:
            return json.dumps(
                {
                    "plan": [
                        {
                            "tool": "add_report_page",
                            "args": {"page": "Main", "display_name": "M"},
                        },
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
                                "measure_name": "Rev",
                                "expression": "SUM(Sales[Amount])",
                            },
                        },
                        {
                            "tool": "add_visual",
                            "args": {
                                "page": "Main",
                                "visual_type": "card",
                                "bindings": {"Values": ["[Rev]"]},
                            },
                        },
                        {
                            "tool": "package_pbip",
                            "args": {
                                "output_path": str(tmp_path / "wire.pbipdir"),
                                "project_name": "WireGenerated",
                                "overwrite": True,
                            },
                        },
                    ]
                }
            )

    monkeypatch.setattr(mcp_module, "StructuredLLMClient", _StubClient)

    server = build_server()
    payload = _call(
        server,
        "generate_report",
        {
            "prompt": "tiny card",
            "workspace": str(tmp_path / "ws"),
            "output_dir": str(tmp_path / "out"),
            "project_name": "WireGenerated",
            "max_cost_usd": 0.0,  # disable budget guard
        },
    )

    assert payload["status"] == "ok", payload
    project_path = Path(payload["project_path"])
    assert project_path.exists()
    assert any(p.name.endswith(".Report") for p in project_path.iterdir())
    assert any(p.name.endswith(".SemanticModel") for p in project_path.iterdir())


def test_generate_report_rejects_empty_prompt_via_wire(tmp_path: Path) -> None:
    """Empty prompt must raise a clear tool-level error carrying
    the underlying ``ValueError`` text."""

    server = build_server()
    with pytest.raises(Exception) as exc_info:
        _call(
            server,
            "generate_report",
            {
                "prompt": "",
                "workspace": str(tmp_path / "ws"),
            },
        )
    assert "non-empty" in str(exc_info.value)
