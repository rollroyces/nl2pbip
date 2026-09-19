"""Model Context Protocol (MCP) server for nl2pbip.

Exposes the nl2pbip pipeline (NL -> PBIP folder) as a small set of MCP
tools so any MCP-compatible client (Claude Code, Cursor, GitHub
Copilot CLI, custom agents) can drive the same deterministic
generator the CLI uses.

Design constraints
------------------

* **stdio transport only (v1).** Every modern MCP client supports it;
  remote/streamable-HTTP can ship in v2 once we have a deployment
  target. The CLI entry point is :func:`main`, invoked by
  ``python -m nl2pbip.mcp_server``.
* **Env-var credentials only.** The server picks up
  ``NL2PBIP_LLM_PROVIDER``, ``OPENAI_API_KEY`` etc. exactly the same
  way the CLI does, so existing deployments need no new config.
* **Four tools.** ``generate_report`` (full NL -> PBIP), ``validate_pbip``
  (round-trip validate an existing folder), ``inspect_dataset``
  (profile a CSV/JSONL/Parquet source before authoring the report),
  ``version`` (server-side capability check). Anything more lives
  behind the Python API until proven necessary.
* **No new LLM call paths.** Each tool delegates to the same
  :class:`Orchestrator`, :class:`PBIRValidator`, and
  :func:`inspect_data_source` the rest of the project already
  exercises. No regression risk to the existing 803-test suite.
* **Lazy ``mcp`` SDK import.** The package is importable without
  ``mcp`` installed; only :func:`build_server` raises an informative
  ``ImportError`` when the optional ``[mcp]`` extra isn't present.
  This keeps ``import nl2pbip.mcp_server`` cheap and lets CI test
  the non-MCP modules without pulling in the optional SDK.

Public API
----------

* :class:`Nl2PbipMcpServer` — wraps a configured :class:`FastMCP`.
* :func:`build_server` — factory used by tests + the CLI entry point.
* :func:`main` — sync stdio entry point (``python -m nl2pbip.mcp_server``).
"""

from __future__ import annotations

from .server import Nl2PbipMcpServer, build_server, main

__all__ = ["Nl2PbipMcpServer", "build_server", "main"]
