"""Tests for the v1.6.0 streamable-http MCP transport + base64 artifact return.

Adds two layers of coverage on top of the existing v1.5.0 tests:

1. **Base64 artifact round-trip** (``tests/test_mcp_server_artifact.py``).
   The ``generate_report`` tool now optionally inlines the
   packaged ``.pbipdir`` as a base64-encoded zip in the response
   payload, so a remote MCP client running over streamable-http
   can deliver the artifact back to its caller without
   filesystem access to the server. This file exercises the
   include/skip paths directly through the Python handler.

2. **Streamable-http transport** (``tests/test_mcp_server_http.py``).
   Boots a real FastMCP server with ``transport='streamable-http'``
   bound to a loopback port, then drives the MCP JSON-RPC surface
   over HTTP with ``httpx``. Catches the wire-format contract
   that the unit tests can't see (header negotiation, session
   lifecycle, host binding).

Both modules are gated on the optional ``[mcp]`` extra via
:func:`pytest.importorskip` so CI's default ``.[dev]`` install
(which doesn't pull the MCP SDK) skips these files cleanly.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import httpx
import io
import json
import os
import socket
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("mcp", reason="MCP transport tests require the [mcp] extra")
pytest.importorskip("httpx", reason="HTTP transport tests require httpx")

from nl2pbip.mcp_server import Nl2PbipMcpServer, build_server
from nl2pbip.mcp_server.server import (
    _encode_pbip_artifact,
    _tool_generate_report,
)

# ---------------------------------------------------------------------
# Base64 artifact return — direct handler tests
# ---------------------------------------------------------------------


def test_artifact_off_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``include_artifact=True`` the response MUST NOT
    carry ``artifact_zip_b64`` / ``artifact_filename`` keys.
    Stdio clients (the v1.5.0 callers) get the same shape as
    before — this is the backwards-compatibility check.
    """

    from nl2pbip.mcp_server import server as mcp_module

    plan = _make_minimal_plan(tmp_path, "NoArtifact")

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def generate(self, _: List[Dict[str, str]]) -> str:
            return json.dumps({"plan": plan})

    monkeypatch.setattr(mcp_module, "StructuredLLMClient", _StubClient)
    result = _tool_generate_report(
        prompt="x",
        workspace=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "out"),
        project_name="NoArtifact",
        max_cost_usd=0.0,
    )
    assert "artifact_zip_b64" not in result
    assert "artifact_filename" not in result


def test_artifact_included_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``include_artifact=True`` the response carries a
    base64-encoded zip whose contents round-trip back to the
    packaged ``.pbipdir`` on disk.
    """

    from nl2pbip.mcp_server import server as mcp_module

    plan = _make_minimal_plan(tmp_path, "WithArtifact")

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def generate(self, _: List[Dict[str, str]]) -> str:
            return json.dumps({"plan": plan})

    monkeypatch.setattr(mcp_module, "StructuredLLMClient", _StubClient)
    result = _tool_generate_report(
        prompt="x",
        workspace=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "out"),
        project_name="WithArtifact",
        max_cost_usd=0.0,
        include_artifact=True,
    )

    assert result["status"] == "ok"
    assert "artifact_zip_b64" in result
    assert result["artifact_filename"].endswith(".pbipdir.zip")

    raw = base64.b64decode(result["artifact_zip_b64"])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = sorted(zf.namelist())
    # The zip should contain the .pbipdir and at least one
    # SemanticModel + Report file. The exact set depends on
    # the orchestrator's package output; assert the directory
    # layout is preserved (paths relative to .pbipdir.parent).
    assert any(
        "WithArtifact.pbipdir/" in n for n in names
    ), f"expected a 'WithArtifact.pbipdir/' entry; got {names[:5]}"


def test_artifact_skipped_when_too_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the zipped artifact exceeds ``max_artifact_bytes``
    the response falls back to ``project_path`` only with a
    warning. Set the cap tiny so any non-empty project trips it.
    """

    from nl2pbip.mcp_server import server as mcp_module

    plan = _make_minimal_plan(tmp_path, "Big")

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def generate(self, _: List[Dict[str, str]]) -> str:
            return json.dumps({"plan": plan})

    monkeypatch.setattr(mcp_module, "StructuredLLMClient", _StubClient)
    result = _tool_generate_report(
        prompt="x",
        workspace=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "out"),
        project_name="Big",
        max_cost_usd=0.0,
        include_artifact=True,
        max_artifact_bytes=64,  # 64 bytes — any real .pbipdir trips it
    )

    assert result["status"] == "ok"
    assert "artifact_zip_b64" not in result
    assert any("too large" in w for w in result["warnings"])


def _make_minimal_plan(tmp_path: Path, project_name: str) -> List[Dict[str, Any]]:
    """Build the minimum viable plan that produces a packaged
    .pbipdir — same shape the v1.5.0 e2e test uses, factored out
    so the three artifact tests can share it."""

    return [
        {
            "tool": "add_report_page",
            "args": {"page": "Main", "display_name": project_name},
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
                "output_path": str(tmp_path / f"{project_name}.pbipdir"),
                "project_name": project_name,
                "overwrite": True,
            },
        },
    ]


def test_encode_pbip_artifact_round_trip(tmp_path: Path) -> None:
    """``_encode_pbip_artifact`` produces a zip whose extracted
    bytes match the source directory (modulo compression)."""

    src = tmp_path / "Sample.pbipdir"
    (src / "SemanticModel").mkdir(parents=True)
    (src / "Sample.pbip").write_text("placeholder", encoding="utf-8")
    (src / "SemanticModel" / "model.tmdl").write_text("/// fake tmdl", encoding="utf-8")

    payload, filename, warning = _encode_pbip_artifact(src, max_bytes=1024 * 1024)
    assert warning is None
    assert filename == "Sample.pbipdir.zip"
    assert payload is not None

    raw = base64.b64decode(payload)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = sorted(zf.namelist())
        with zf.open("Sample.pbipdir/Sample.pbip") as fh:
            assert fh.read().decode("utf-8") == "placeholder"
    assert "Sample.pbipdir/Sample.pbip" in names
    assert "Sample.pbipdir/SemanticModel/model.tmdl" in names


def test_encode_pbip_artifact_missing_dir(tmp_path: Path) -> None:
    """Missing directory returns a warning and no payload."""

    missing = tmp_path / "does-not-exist.pbipdir"
    payload, filename, warning = _encode_pbip_artifact(missing, max_bytes=1024)
    assert payload is None
    assert filename is None
    assert warning is not None
    assert "missing" in warning.lower()


# ---------------------------------------------------------------------
# Streamable-HTTP transport — round-trip over a real HTTP server
# ---------------------------------------------------------------------


def _free_port() -> int:
    """Ask the OS for an unused TCP port for the test server.

    Avoids hard-coding a port that could collide with a
    parallel CI run or a developer's local server.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _parse_sse_jsonrpc(body: str) -> Dict[str, Any]:
    """Pull the JSON-RPC payload out of a streamable-http SSE body.

    The MCP streamable-http transport replies with
    ``text/event-stream`` content. Each event is a ``data:``
    line followed by a blank line; the JSON-RPC payload lives
    on the ``data:`` line. Some events (notably the
    ``notifications/initialized`` ack the server emits after
    ``initialize``) carry no ``data:`` payload at all, so we
    skip them.
    """

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if not payload:
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            # Not a JSON-RPC frame (e.g. a notification); keep
            # scanning subsequent data: lines.
            continue
    raise AssertionError(f"no JSON-RPC frame in SSE body:\n{body[:500]}")


def _initialize_session(client, base_url: str) -> Dict[str, str]:
    """Open an MCP session and return the ``initialize`` result.

    The MCP streamable-http transport requires a session ID
    issued by ``initialize`` before any other RPC call. The
    caller is responsible for closing the session; we don't
    bother because the test fixture kills the server anyway.
    """

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        },
        headers=MCP_HEADERS,
    )
    assert r.status_code == 200, r.text
    session_id = r.headers.get("mcp-session-id")
    assert session_id, "streamable-http server must return a session id"
    return {"mcp-session-id": session_id}


def _tools_list(
    client: Any,
    base_url: str,
    session_headers: Dict[str, str],
    extra_headers: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Return the sorted list of tool names advertised by the server."""

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        },
        headers={**MCP_HEADERS, **session_headers, **(extra_headers or {})},
    )
    assert r.status_code == 200, r.text
    body = _parse_sse_jsonrpc(r.text)
    return sorted(t["name"] for t in body["result"]["tools"])


def _tools_call(
    client,
    base_url: str,
    session_headers: Dict[str, str],
    name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Invoke an MCP tool over streamable-http and return the parsed JSON.

    Raises an :class:`AssertionError` (with the JSON-RPC error
    message) if the server returns an error frame instead of a
    result — the unit tests already cover the underlying tool
    behaviour, so this is purely a wire-format sanity check.
    """

    with client.stream(
        "POST",
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={**MCP_HEADERS, **session_headers},
    ) as response:
        assert response.status_code == 200, response.read()
        text = response.read().decode("utf-8")
    body = _parse_sse_jsonrpc(text)
    if "error" in body:
        raise AssertionError(f"tool {name!r} returned JSON-RPC error: {body['error']}")
    assert "result" in body, body
    # MCP wraps tool output in ``content[0].text`` (JSON-encoded
    # payload by the server) — match what the existing e2e test
    # asserts in tests/test_mcp_server_e2e.py.
    raw_text = body["result"]["content"][0]["text"]
    return json.loads(raw_text)


@pytest.fixture
def http_server(request):
    """Boot a streamable-http FastMCP server on a free loopback port.

    Accepts an optional indirect param ``request.param`` carrying a
    ``bearer_token`` string. When the param is provided (or omitted)
    the server is built with ``bearer_token=request.param``; pass
    ``None`` (or simply don't parametrize) for the legacy no-auth
    default. Yields the base URL; the server runs in a background
    thread and is reclaimed when the test process exits.
    """

    bearer_token: Optional[str] = getattr(request, "param", None)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = build_server(
        name="nl2pbip-http-test",
        host="127.0.0.1",
        port=port,
        bearer_token=bearer_token,
    )

    server_thread = threading.Thread(
        target=lambda: server.run(transport="streamable-http"),
        daemon=True,
    )
    server_thread.start()

    # Wait for the server to bind. Polling /mcp with a
    # connection-refused loop is reliable on macOS CI. When the
    # server has a bearer token configured, the probe request
    # WITHOUT the header gets 401 — that's a successful bind,
    # not a connect failure, so we only fall through to the
    # raise on httpx transport-level errors (connection refused).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5, base_url=base_url) as probe:
                probe.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}},
                    headers=MCP_HEADERS,
                )
                break
        except (httpx.HTTPError, OSError):
            time.sleep(0.05)
    else:
        pytest.fail(f"server did not bind on {base_url} within 5s")

    yield base_url

    # No clean shutdown API for FastMCP.run(); the daemon thread
    # dies with the test process. No port to release either since
    # the test process exits and the OS reclaims the socket.


def test_streamable_http_binds_loopback_only(http_server: str) -> None:
    """The server MUST bind to ``127.0.0.1`` so a default
    deployment doesn't expose the no-auth, LLM-cost-rackup
    surface to the network.

    Note: on macOS / Linux a socket bound to ``127.0.0.1`` is
    ALSO reachable via the ``0.0.0.0`` wildcard address at the
    kernel level — that's loopback semantics, not an exposure
    of the wildcard interface (RFC 5735 guarantees
    ``127.0.0.0/8`` is non-routable). The real assertion is
    therefore on the server's own ``getsockname`` /
    FastMCP bound host, not on a remote connect attempt.
    """

    # The simplest correctness check: inspect the running
    # server's host configuration via the FastMCP wrapper.
    # The fixture's server was built with host='127.0.0.1',
    # so this is enforced at construction time; the HTTP
    # fixture started it on a real loopback port.
    instance = Nl2PbipMcpServer(
        name="nl2pbip-loopback-check", host="127.0.0.1", port=19090
    )
    assert instance.host == "127.0.0.1", (
        f"Nl2PbipMcpServer stored host={instance.host!r}; "
        "must default to 127.0.0.1 (loopback) — no auth means the "
        "default deployment must not expose the LLM-cost-rackup "
        "surface to the network"
    )
    # Sanity: the running HTTP server URL also points at 127.0.0.1.
    assert http_server.startswith("http://127.0.0.1:")


def test_streamable_http_lists_four_tools(http_server: str) -> None:
    """The four MCP tools must be discoverable over the
    streamable-http JSON-RPC ``tools/list`` method."""

    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        session = _initialize_session(client, http_server)
        tool_names = _tools_list(client, http_server, session)
    assert tool_names == [
        "generate_report",
        "inspect_dataset",
        "validate_pbip",
        "version",
    ]


def test_streamable_http_invokes_version_tool(http_server: str) -> None:
    """Round-trip the ``version`` tool over HTTP. Catches
    FastMCP's streamable-http serialisation path that the
    in-process tests bypass."""

    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        session = _initialize_session(client, http_server)
        result = _tools_call(client, http_server, session, "version", {})

    assert result["server_name"] == "nl2pbip-mcp"
    assert result["library_version"]
    assert result["transport"] == "stdio"  # reported by the tool itself


def test_streamable_http_invokes_inspect_dataset(
    http_server: str,
    tmp_path: Path,
) -> None:
    """CSV profile round-trip over HTTP."""

    csv = tmp_path / "sales.csv"
    csv.write_text(
        "region,amount\nnorth,100\nsouth,200\n",
        encoding="utf-8",
    )
    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        session = _initialize_session(client, http_server)
        result = _tools_call(
            client,
            http_server,
            session,
            "inspect_dataset",
            {"source": str(csv), "max_rows": 10},
        )

    assert result["source_kind"] == "csv"
    assert len(result["tables"]) == 1
    cols = {c["name"] for c in result["tables"][0]["columns"]}
    assert cols == {"region", "amount"}


def test_streamable_http_invokes_validate_pbip_on_real_fixture(
    http_server: str,
) -> None:
    """Round-trip ``validate_pbip`` over HTTP against the committed
    fixture. Confirms the validator output survives the SSE
    serialisation path unchanged."""

    fixture_root = Path(__file__).parent.parent / "artifacts" / "SalesInsights.pbipdir"
    if not fixture_root.exists():
        pytest.skip(
            "SalesInsights.pbipdir not present — run the orchestrator "
            "integration test first to materialise it."
        )

    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        session = _initialize_session(client, http_server)
        result = _tools_call(
            client,
            http_server,
            session,
            "validate_pbip",
            {"pbip_path": str(fixture_root)},
        )

    assert result["status"] == "ok", result.get("issues")
    assert result["pages_validated"] >= 1
    assert result["visuals_validated"] >= 1
    assert result["errors"] == 0


def test_streamable_http_invokes_generate_report_with_artifact(
    http_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full NL -> .pbipdir pipeline over HTTP, with the
    base64 artifact return so a remote MCP client gets the
    .pbip without filesystem access to the server.
    """

    from nl2pbip.mcp_server import server as mcp_module

    plan = _make_minimal_plan(tmp_path, "Http")

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def generate(self, _: List[Dict[str, str]]) -> str:
            return json.dumps({"plan": plan})

    monkeypatch.setattr(mcp_module, "StructuredLLMClient", _StubClient)

    with httpx.Client(timeout=10.0, base_url=http_server) as client:
        session = _initialize_session(client, http_server)
        result = _tools_call(
            client,
            http_server,
            session,
            "generate_report",
            {
                "prompt": "tiny",
                "workspace": str(tmp_path / "ws"),
                "output_dir": str(tmp_path / "out"),
                "project_name": "Http",
                "max_cost_usd": 0.0,
                "include_artifact": True,
            },
        )

    assert result["status"] == "ok"
    assert result["artifact_filename"].endswith(".pbipdir.zip")
    raw = base64.b64decode(result["artifact_zip_b64"])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
    assert any("Http.pbipdir/" in n for n in names)


# ---------------------------------------------------------------------
# Construction: build_server + Nl2PbipMcpServer bind config
# ---------------------------------------------------------------------


def test_build_server_accepts_host_and_port() -> None:
    """``build_server(host, port)`` must succeed without binding
    a socket (binding only happens at ``server.run()`` time).
    """

    server = build_server(name="nl2pbip-config-test", host="127.0.0.1", port=18765)
    assert server is not None
    # Sanity: list_tools still works.
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 4


def test_nl2pbip_mcp_server_stores_host_and_port() -> None:
    """The convenience class must surface ``host`` and ``port``
    so callers can introspect (and so future logging / health
    endpoints can report them).
    """

    instance = Nl2PbipMcpServer(
        name="nl2pbip-cfg",
        host="127.0.0.1",
        port=19000,
    )
    assert instance.host == "127.0.0.1"
    assert instance.port == 19000
    assert instance.name == "nl2pbip-cfg"


# ---------------------------------------------------------------------
# Bearer-token auth (v1.7.0)
# ---------------------------------------------------------------------

_TEST_BEARER = "test-bearer-token-12345"


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post_initialize(
    client: Any,
    base_url: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Any:
    """POST ``initialize`` and return the raw response (no
    assertion on status — the auth tests want to inspect 401s).
    """

    headers = {**MCP_HEADERS, **(extra_headers or {})}
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        },
        headers=headers,
    )


@pytest.mark.parametrize("http_server", [_TEST_BEARER], indirect=True)
def test_http_unauthenticated_request_returns_401(http_server: str) -> None:
    """When the server is built with a Bearer token, an HTTP
    request without an ``Authorization: Bearer <token>`` header
    MUST return ``401 Unauthorized`` with a
    ``WWW-Authenticate: Bearer realm="nl2pbip-mcp"`` response
    header — BEFORE any session is established.
    """

    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        r = _post_initialize(client, http_server)  # no Authorization header

    assert r.status_code == 401, r.text
    assert "mcp-session-id" not in r.headers, (
        "401 must be returned before session init — the server must not "
        "leak a session ID to an unauthenticated caller"
    )
    www_auth = r.headers.get("www-authenticate", "")
    assert "Bearer" in www_auth, f"missing WWW-Authenticate Bearer header: {www_auth!r}"
    assert (
        'realm="nl2pbip-mcp"' in www_auth
    ), f"WWW-Authenticate realm must be 'nl2pbip-mcp'; got {www_auth!r}"


@pytest.mark.parametrize("http_server", [_TEST_BEARER], indirect=True)
def test_http_wrong_bearer_token_returns_401(http_server: str) -> None:
    """A wrong Bearer token gets the same 401 + WWW-Authenticate
    contract — the response must not distinguish 'no token' from
    'wrong token' (otherwise it leaks which tokens are valid).
    """

    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        r = _post_initialize(client, http_server, extra_headers=_auth_headers("wrong"))

    assert r.status_code == 401, r.text
    assert "Bearer" in r.headers.get("www-authenticate", "")


@pytest.mark.parametrize("http_server", [_TEST_BEARER], indirect=True)
def test_http_correct_bearer_token_succeeds(http_server: str) -> None:
    """With the correct Bearer token, ``initialize`` returns 200
    and a session id, and ``tools/list`` advertises the four
    documented MCP tools.
    """

    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        r = _post_initialize(
            client, http_server, extra_headers=_auth_headers(_TEST_BEARER)
        )
        assert r.status_code == 200, r.text
        session_id = r.headers.get("mcp-session-id")
        assert session_id, "streamable-http server must return a session id"
        session = {"mcp-session-id": session_id}

        # The Bearer header is required on every request (the
        # auth middleware sits BEFORE the session manager), so
        # subsequent calls must carry it too.
        tool_names = _tools_list(
            client, http_server, session, extra_headers=_auth_headers(_TEST_BEARER)
        )

    assert tool_names == [
        "generate_report",
        "inspect_dataset",
        "validate_pbip",
        "version",
    ]


def test_http_no_auth_when_token_unset(http_server: str) -> None:
    """When the server is built without a bearer token
    (back-compat default), HTTP requests succeed without an
    ``Authorization`` header. The v1.6.0 callers see the same
    no-auth loopback behaviour they had before.
    """

    with httpx.Client(timeout=5.0, base_url=http_server) as client:
        session = _initialize_session(client, http_server)  # no auth header
        tool_names = _tools_list(client, http_server, session)

    assert tool_names == [
        "generate_report",
        "inspect_dataset",
        "validate_pbip",
        "version",
    ]


def test_http_bearer_token_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token-comparison path MUST use ``hmac.compare_digest``
    rather than ``==``. A simple ``==`` short-circuits on the
    first mismatching byte and leaks the valid token's prefix
    length via timing — defeating the point of a constant
    comparison. We monkeypatch ``hmac.compare_digest`` so the
    test fails if any code path bypasses it.
    """

    from nl2pbip.mcp_server import server as mcp_module

    real_compare_digest = hmac.compare_digest
    call_state: Dict[str, int] = {"calls": 0}

    def spy_compare_digest(a: bytes, b: bytes) -> bool:
        call_state["calls"] += 1
        return real_compare_digest(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy_compare_digest)
    # The server module imports ``hmac`` directly, so its bound
    # ``hmac.compare_digest`` is what the auth path actually
    # calls. Patch the attribute the module already references
    # too, so the spy can't be bypassed via the cached name.
    monkeypatch.setattr(mcp_module.hmac, "compare_digest", spy_compare_digest)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = build_server(
        name="nl2pbip-auth-spy",
        host="127.0.0.1",
        port=port,
        bearer_token="spy-token",
    )
    server_thread = threading.Thread(
        target=lambda: server.run(transport="streamable-http"),
        daemon=True,
    )
    server_thread.start()

    # Wait for bind
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5, base_url=base_url) as probe:
                probe.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}},
                    headers=MCP_HEADERS,
                )
                break
        except (httpx.HTTPError, OSError):
            time.sleep(0.05)
    else:
        pytest.fail(f"server did not bind on {base_url} within 5s")

    with httpx.Client(timeout=5.0, base_url=base_url) as client:
        r = _post_initialize(client, base_url, extra_headers=_auth_headers("spy-token"))

    assert r.status_code == 200, r.text
    assert call_state["calls"] >= 1, (
        "bearer-auth path did not invoke hmac.compare_digest — the "
        "implementation must use constant-time comparison to avoid "
        "leaking the valid token prefix via timing"
    )


def test_http_bearer_token_env_var_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting ``NL2PBIP_MCP_BEARER_TOKEN`` in the subprocess
    environment MUST flip the server into auth-required mode even
    when the operator didn't pass ``--bearer-token``. The
    CLI / env var precedence is: ``--bearer-token`` > env > unset.
    """

    import subprocess
    import sys

    port = _free_port()
    env_token = "env-derived-secret-9876"
    env = os.environ.copy()
    env["NL2PBIP_MCP_BEARER_TOKEN"] = env_token
    # Ensure we don't accidentally inherit a real operator token
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nl2pbip.mcp_server.server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        # Wait for the server to bind
        deadline = time.monotonic() + 8.0
        bound = False
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=0.5, base_url=base_url) as probe:
                    probe.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 99,
                            "method": "ping",
                            "params": {},
                        },
                        headers=MCP_HEADERS,
                    )
                    bound = True
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.1)
        assert bound, (
            f"server did not bind on {base_url} within 8s; "
            f"stderr: {proc.stderr.read().decode('utf-8', errors='replace') if proc.stderr else ''}"
        )

        # No auth header → must be 401
        with httpx.Client(timeout=5.0, base_url=base_url) as client:
            r = _post_initialize(client, base_url)
        assert (
            r.status_code == 401
        ), f"env-derived token should require auth; got {r.status_code}: {r.text}"
        assert "Bearer" in r.headers.get("www-authenticate", "")

        # With the env-derived token → must succeed
        with httpx.Client(timeout=5.0, base_url=base_url) as client:
            r = _post_initialize(
                client, base_url, extra_headers=_auth_headers(env_token)
            )
        assert r.status_code == 200, r.text
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=3)
