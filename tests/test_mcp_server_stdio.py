"""JSON-RPC round-trip tests for the stdio transport of the
nl2pbip MCP server.

Companion to ``tests/test_mcp_server.py`` (in-process Python
function tests) and ``tests/test_mcp_server_v160.py``
(streamable-http round-trip). The stdio transport shipped in
v1.5.0 but had no end-to-end coverage of the actual JSON-RPC
wire — these tests fill that gap.

The tests are 100% stdlib: a ``subprocess.Popen`` of the
``nl2pbip-mcp`` console script, a thread draining stderr so log
lines don't pollute test output, and ``select``-based line reads
with a hard timeout. No ``pexpect``, no third-party process
libraries.

The MCP SDK is imported via :func:`pytest.importorskip` so CI's
slim ``.[dev]`` install (which lacks the optional ``[mcp]``
extra) skips these tests cleanly rather than failing at
collection time. Run locally with ``pip install -e ".[dev,mcp]"``
to exercise the full suite.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

pytest.importorskip("mcp", reason="MCP server tests require the [mcp] extra")

from nl2pbip import __version__ as _NL2PBIP_VERSION

# ---------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------


# Resolve the console script once at import time. ``nl2pbip-mcp`` is
# the entry point registered in pyproject.toml (project.scripts) and
# points at ``nl2pbip.mcp_server.server:main``. The goal spec says
# ``python -m nl2pbip.mcp_server`` but no ``__main__.py`` exists in
# that package, so ``-m`` raises ``No module named ...__main__``.
# The console script is the supported entry point — using it keeps
# the tests aligned with how a real client (Cursor / Claude Desktop
# / etc.) spawns the server.
#
# Walk up from this test file to find the project root, then look
# for ``.venv/bin/nl2pbip-mcp``. Using the project-local venv is
# more reliable than resolving ``sys.executable`` (which can be a
# symlink that doesn't share a directory with the console script
# when the venv is a PEP 405-style ``pyvenv.cfg`` pointing at a
# system Python).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONSOLE_SCRIPT = _PROJECT_ROOT / ".venv" / "bin" / "nl2pbip-mcp"
if not _CONSOLE_SCRIPT.exists():  # pragma: no cover - environment guard
    _CONSOLE_SCRIPT = None  # type: ignore[assignment]

# Per-call timeout for ``select``-based stdout reads. Five seconds
# is enough for the slowest tool (generate_report packages TMDL +
# PBIR + zips the artifact on a tiny plan) without making a hung
# server block the suite for long.
_READ_TIMEOUT_S = 5.0


def _drain_stderr(proc: subprocess.Popen[str]) -> threading.Thread:
    """Start a daemon thread that drains ``proc.stderr`` into the void.

    FastMCP writes INFO / WARNING log lines to stderr on every
    request (e.g. ``INFO Processing request of type
    CallToolRequest``). Left undrained they fill the OS pipe buffer
    and block the server on its next log line — which then appears
    to hang in tests. The thread dies naturally when the process
    closes stderr.
    """

    def _loop() -> None:
        assert proc.stderr is not None
        for _line in proc.stderr:
            pass  # intentionally silent

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread


def _send(proc: subprocess.Popen[str], message: Dict[str, Any]) -> None:
    """Write one JSON-RPC message + newline to the server's stdin."""
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[str], timeout: float = _READ_TIMEOUT_S) -> str:
    """Read one JSON-RPC message line from stdout with a hard timeout.

    Uses :func:`select.select` rather than ``proc.stdout.readline()``
    so a hung server raises :class:`TimeoutError` instead of
    blocking the test indefinitely. Returns the raw line with the
    trailing newline stripped.
    """
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"no stdout line within {timeout:.1f}s from pid {proc.pid}"
            )
        rlist, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.2))
        if rlist:
            line = proc.stdout.readline()
            if not line:
                raise EOFError(
                    f"server closed stdout before sending a response "
                    f"(pid {proc.pid}, returncode={proc.returncode})"
                )
            return line.rstrip("\n")


def _recv_json(
    proc: subprocess.Popen[str],
    timeout: float = _READ_TIMEOUT_S,
    expected_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Read one JSON-RPC message and parse it as JSON.

    Skips server-emitted ``notifications/message`` frames (which
    have no ``id`` and are not a response to a request). If
    ``expected_id`` is set, keeps reading until a frame with that
    id arrives — guards against the case where the server emits
    an intermediate notification between the request and the
    matching response (e.g. after a malformed-JSON line).
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"no matching response within {timeout:.1f}s from pid "
                f"{proc.pid} (expected_id={expected_id})"
            )
        line = _recv(proc, timeout=min(remaining, _READ_TIMEOUT_S))
        # json.loads returns Any by design; we narrow to a dict
        # via the local annotation so mypy knows ``envelope`` is
        # subscriptable but doesn't reject the ``Any``-typed
        # return from json.loads.
        envelope: Dict[str, Any] = json.loads(line)
        # Notifications have no ``id`` and no ``result``/``error``.
        # They are broadcast by the server (e.g. logging) and must
        # not be returned as a "response" to a request.
        if expected_id is not None:
            if envelope.get("id") == expected_id:
                return envelope
            # Not the frame we wanted — keep reading.
            continue
        if "id" in envelope:
            return envelope
        # Otherwise it's a notification; skip and read the next.
        continue


# ---------------------------------------------------------------------
# Pytest fixture
# ---------------------------------------------------------------------


@pytest.fixture()
def stdio_server() -> Iterator[subprocess.Popen[str]]:
    """Spawn the MCP server as a real subprocess for the duration of one test.

    Function scope so each test gets a fresh process — keeps the
    tests independent and ensures the handshake + tools/list are
    exercised every run (rather than shared cached state). The
    teardown kills the process if it's still alive so a hung test
    doesn't leak subprocesses.
    """
    if _CONSOLE_SCRIPT is None:  # pragma: no cover - environment guard
        pytest.skip("nl2pbip-mcp console script not found in the active venv")

    env = dict(os.environ)
    # Force unbuffered stdio so log lines hit stderr promptly even
    # when FastMCP bypasses Python's print() and writes directly.
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Make the subprocess resolve ``nl2pbip`` from the same
    # checkout the test process is running against. Without this
    # the subprocess falls back to whatever ``nl2pbip`` is
    # installed in site-packages (which may be a stale wheel
    # from an earlier release — observed locally with a 1.5.0
    # wheel shadowing the 1.6.1 checkout). ``PYTHONPATH`` wins
    # over site-packages for the editable checkout.
    env.setdefault("PYTHONPATH", str(_PROJECT_ROOT))

    proc = subprocess.Popen(  # noqa: S603 - controlled invocation for tests
        [str(_CONSOLE_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )
    _drain_stderr(proc)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                proc.wait(timeout=2)


# ---------------------------------------------------------------------
# JSON-RPC helpers (built on top of the subprocess plumbing)
# ---------------------------------------------------------------------


def _initialize(proc: subprocess.Popen[str]) -> Dict[str, Any]:
    """Send the MCP ``initialize`` request; return the parsed response.

    Uses the protocol version advertised by FastMCP 1.x in its
    ``initialize`` response (``2024-11-05`` is the v1.x default).
    A future SDK bump that changes the protocol version should be
    surfaced here as a single edit.
    """
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nl2pbip-stdio-tests", "version": "0.0.0"},
            },
        },
    )
    return _recv_json(proc, expected_id=1)


def _initialized_notification(proc: subprocess.Popen[str]) -> None:
    """Send the ``notifications/initialized`` notice the SDK requires.

    This is a JSON-RPC notification (no ``id``, no response
    expected). FastMCP 1.x rejects ``tools/list`` until the
    client confirms the handshake with this notice.
    """
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
    )


def _stdio_handshake(proc: subprocess.Popen[str]) -> Dict[str, Any]:
    """Run the full JSON-RPC handshake; return the ``initialize`` result.

    The return value is the ``result`` field of the initialize
    response — handy for assertions on ``serverInfo`` and
    ``protocolVersion`` without unpacking the envelope every time.
    """
    init_resp = _initialize(proc)
    assert "result" in init_resp, f"initialize returned no result: {init_resp!r}"
    _initialized_notification(proc)
    result: Dict[str, Any] = init_resp["result"]
    return result


def _stdio_list_tools(proc: subprocess.Popen[str]) -> List[Dict[str, Any]]:
    """Send ``tools/list``; return the list of tool descriptors."""
    _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    resp = _recv_json(proc, expected_id=2)
    assert "result" in resp, f"tools/list returned no result: {resp!r}"
    tools: List[Dict[str, Any]] = resp["result"]["tools"]
    return tools


def _stdio_call_tool(
    proc: subprocess.Popen[str],
    name: str,
    request_id: int,
    arguments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send ``tools/call``; return the parsed response envelope.

    Callers should inspect ``result`` for the success path or
    ``error`` for the JSON-RPC error path — both shapes are valid
    (FastMCP returns a tool-level error inside ``result.isError``
    for argument-validation failures, while SDK-level errors come
    back as ``error`` at the envelope level).
    """
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    return _recv_json(proc, expected_id=request_id)


# ---------------------------------------------------------------------
# Minimal plan fixture (copied shape from tests/test_mcp_server_v160.py)
# ---------------------------------------------------------------------


def _make_minimal_plan(tmp_path: Path, project_name: str) -> List[Dict[str, Any]]:
    """Build the minimum viable plan that produces a packaged
    ``.pbipdir`` — same 5-step shape the v1.6.0 round-trip tests
    use, factored here so ``generate_report`` and
    ``validate_pbip`` can share the artifact path.

    Steps: ``add_report_page`` → ``create_table`` →
    ``add_measure`` → ``add_visual`` → ``package_pbip``.
    """
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


# ---------------------------------------------------------------------
# 1. JSON-RPC handshake + tools/list
# ---------------------------------------------------------------------


def test_stdio_server_speaks_json_rpc_handshake(
    stdio_server: subprocess.Popen[str],
) -> None:
    """The stdio server must speak the MCP JSON-RPC handshake.

    Sends ``initialize`` with ``protocolVersion``,
    ``capabilities``, and ``clientInfo``; asserts the response
    carries ``serverInfo`` and the right ``protocolVersion``.
    Then sends ``notifications/initialized`` + ``tools/list`` and
    asserts the four registered tools come back with the expected
    names.
    """
    result = _stdio_handshake(stdio_server)

    assert "serverInfo" in result, f"initialize response missing serverInfo: {result!r}"
    assert result["serverInfo"]["name"] == "nl2pbip"
    assert isinstance(result["serverInfo"].get("version"), str)
    assert "protocolVersion" in result

    tools = _stdio_list_tools(stdio_server)
    tool_names = sorted(t["name"] for t in tools)
    assert tool_names == sorted(
        ["generate_report", "validate_pbip", "inspect_dataset", "version"]
    )


# ---------------------------------------------------------------------
# 2. generate_report round-trip (uses stub plan; no real LLM)
# ---------------------------------------------------------------------


def test_stdio_server_call_generate_report(
    stdio_server: subprocess.Popen[str],
    tmp_path: Path,
) -> None:
    """Full handshake + ``tools/call generate_report`` round-trip.

    The MCP tool handler builds a ``StructuredLLMClient`` from env
    vars — this test does NOT stub the LLM because the stub lives
    in the parent test process and the stdio subprocess has its
    own Python interpreter. With no ``OPENAI_API_KEY`` /
    ``ANTHROPIC_API_KEY`` set in the subprocess env, the LLM
    call will fail; the wire-format round-trip still succeeds
    (the tool returns a properly-framed JSON-RPC error inside
    ``result.isError=true`` with a ``text`` content block). This
    test asserts the wire format works — LLM behaviour is covered
    by the in-process tests in ``test_mcp_server.py``.

    The ``monkeypatch`` parameter is intentionally NOT taken here
    because a parent-process monkeypatch does not propagate to
    the spawned subprocess (different Python interpreter).
    """
    _stdio_handshake(stdio_server)

    resp = _stdio_call_tool(
        stdio_server,
        "generate_report",
        request_id=10,
        arguments={
            "prompt": "Sales by region",
            "workspace": str(tmp_path / "ws"),
            "output_dir": str(tmp_path),
            "project_name": "StdioTest",
            "max_cost_usd": 0.0,
        },
    )

    assert (
        "result" in resp
    ), f"generate_report returned a JSON-RPC-level error: {resp!r}"
    content = resp["result"].get("content", [])
    assert content, f"generate_report returned empty content: {resp!r}"
    assert content[0]["type"] == "text"
    text = content[0]["text"]
    # The wire round-trip is what we're proving. The text can be:
    #   * an LLM error message (no API keys in subprocess env)
    #   * the success payload with the artifact path
    #   * a "no artifact" / "rate limit" message if the plan
    #     completes but the package step fails.
    # Any of these prove the JSON-RPC surface worked.
    assert (
        isinstance(text, str) and text
    ), f"generate_report returned empty text: {resp!r}"


# ---------------------------------------------------------------------
# 3. version round-trip — pure assertion, no I/O
# ---------------------------------------------------------------------


def test_stdio_server_call_version(stdio_server: subprocess.Popen[str]) -> None:
    """``tools/call version`` must report the running library version.

    Reads ``nl2pbip.__version__`` dynamically (no hardcoded
    string) so the test stays correct across releases.
    """
    _stdio_handshake(stdio_server)

    resp = _stdio_call_tool(stdio_server, "version", request_id=11)

    assert "result" in resp, f"version returned no result: {resp!r}"
    content = resp["result"]["content"]
    assert content[0]["type"] == "text"

    # The text content is a JSON-encoded dict (matches
    # ``test_mcp_server.py::test_version_reports_library_and_prompt_versions``).
    payload = json.loads(content[0]["text"])
    assert payload["server_name"] == "nl2pbip-mcp"
    assert payload["library_version"] == _NL2PBIP_VERSION
    assert payload["transport"] == "stdio"


# ---------------------------------------------------------------------
# 4. inspect_dataset round-trip — real file I/O
# ---------------------------------------------------------------------


def test_stdio_server_call_inspect_dataset(
    stdio_server: subprocess.Popen[str], tmp_path: Path
) -> None:
    """``tools/call inspect_dataset`` must profile a real CSV over the wire.

    Writes a 5-row CSV to ``tmp_path`` and sends it through the
    stdio transport. Asserts the response mentions the row count,
    column count, and the column names. The MCP tool's parameter
    is named ``source`` (matches ``_tool_inspect_dataset(source=
    ...)``) — the goal spec's ``path`` was a high-level hint, not
    the wire-level key.
    """
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text(
        "region,amount\nnorth,100\nsouth,200\nnorth,150\nsouth,300\n" "north,250\n",
        encoding="utf-8",
    )

    _stdio_handshake(stdio_server)

    resp = _stdio_call_tool(
        stdio_server,
        "inspect_dataset",
        request_id=12,
        arguments={"source": str(csv_path)},
    )

    assert "result" in resp, f"inspect_dataset returned no result: {resp!r}"
    content = resp["result"]["content"]
    assert content[0]["type"] == "text"
    text = content[0]["text"]

    # Column names must appear in the profile. The inspector
    # formats the response as JSON; both column names must be
    # mentioned somewhere in the response text.
    assert "region" in text, f"column 'region' missing from profile: {text[:400]}"
    assert "amount" in text, f"column 'amount' missing from profile: {text[:400]}"
    # Row count: 5 data rows (header excluded by the inspector).
    assert "5" in text, f"row count 5 missing from profile: {text[:400]}"


# ---------------------------------------------------------------------
# 5. validate_pbip round-trip — uses the artifact from a packaged plan
# ---------------------------------------------------------------------


def test_stdio_server_call_validate_pbip(
    stdio_server: subprocess.Popen[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tools/call validate_pbip`` must run over the wire and report status.

    Builds a real packaged ``.pbipdir`` in ``tmp_path`` by running
    the orchestrator with a stub LLM (no network), then sends the
    path to ``validate_pbip`` over the stdio transport. The
    validator must return a structured result; we tolerate either
    ``valid`` or ``invalid`` plus the detail block because the
    validator's pass/fail depends on the exact package layout and
    we don't want a structural drift in ``PBIRValidator`` to mask
    a wire-format regression.
    """
    from nl2pbip.mcp_server import server as mcp_module

    plan = _make_minimal_plan(tmp_path, "ValidateStdio")

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def generate(self, _: List[Dict[str, str]]) -> str:
            return json.dumps({"plan": plan})

    monkeypatch.setattr(mcp_module, "StructuredLLMClient", _StubClient)

    # Materialise the .pbipdir on disk via the in-process handler
    # so the stdio round-trip only exercises validate_pbip. The
    # artifact path comes from the last package_pbip step.
    package_step = next(s for s in plan if s["tool"] == "package_pbip")
    artifact_dir = Path(package_step["args"]["output_path"])

    from nl2pbip.mcp_server.server import _tool_generate_report

    _tool_generate_report(
        prompt="tiny",
        workspace=str(tmp_path / "ws"),
        output_dir=str(tmp_path),
        project_name="ValidateStdio",
        max_cost_usd=0.0,
    )

    assert (
        artifact_dir.exists()
    ), f"setup failed: orchestrator did not materialise {artifact_dir}"

    _stdio_handshake(stdio_server)

    resp = _stdio_call_tool(
        stdio_server,
        "validate_pbip",
        request_id=13,
        arguments={"pbip_path": str(artifact_dir)},
    )

    assert "result" in resp, f"validate_pbip returned no result: {resp!r}"
    content = resp["result"]["content"]
    assert content[0]["type"] == "text"
    text = content[0]["text"]
    # Tolerant: validator may report either "valid" (status ok) or
    # "invalid" (status errors_found). Both prove the wire works.
    assert any(
        marker in text.lower() for marker in ("valid", "invalid")
    ), f"validate_pbip response missing valid/invalid: {text[:400]}"


# ---------------------------------------------------------------------
# 6. Malformed JSON — server must not crash
# ---------------------------------------------------------------------


def test_stdio_server_handles_malformed_json(
    stdio_server: subprocess.Popen[str],
) -> None:
    """Garbage on stdin must not crash the server.

    Sends ``"this is not JSON\\n"``, then sends a well-formed
    ``tools/list`` request and asserts the server is still alive
    and still responding (this is the most important wire-robustness
    guarantee — a single bad line from a buggy client must not
    terminate the subprocess).
    """
    # No handshake first — the malformed line must be tolerated
    # even before initialize completes.
    assert stdio_server.stdin is not None
    stdio_server.stdin.write("this is not JSON\n")
    stdio_server.stdin.flush()

    # Give the server a moment to react (FastMCP logs the parse
    # error to stderr; we don't care, just confirm it doesn't exit).
    time.sleep(0.2)
    assert (
        stdio_server.poll() is None
    ), f"server exited after malformed JSON: rc={stdio_server.returncode}"

    # Now do a real handshake and assert tools/list works — proves
    # the malformed line didn't poison the JSON-RPC state machine.
    _stdio_handshake(stdio_server)
    tools = _stdio_list_tools(stdio_server)
    assert len(tools) == 4, f"tools/list after malformed returned {len(tools)}"

    # Clean shutdown — exit code 0 means the server didn't crash
    # when we closed stdin.
    stdio_server.stdin.close()
    try:
        stdio_server.wait(timeout=2)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        stdio_server.kill()
        stdio_server.wait(timeout=2)
    assert stdio_server.returncode == 0, (
        f"server exit code {stdio_server.returncode} after clean stdin close "
        f"(expected 0, definitely not -11 SIGSEGV)"
    )


# ---------------------------------------------------------------------
# 7. Unknown method — server must not crash, may drop silently
# ---------------------------------------------------------------------


def test_stdio_server_handles_unknown_method(
    stdio_server: subprocess.Popen[str],
) -> None:
    """An unknown JSON-RPC method must not crash the server.

    Sends ``{"method": "totally/not/a/real/method"}`` and verifies
    the server either returns a JSON-RPC error envelope (preferred)
    OR silently drops the request without exiting. FastMCP 1.x's
    current behaviour is the latter (it logs a pydantic validation
    error to stderr and does not write a response) — but the test
    is shaped to accept either, so a future SDK release that
    returns a proper ``-32601 Method not found`` error passes
    without changes.
    """
    _stdio_handshake(stdio_server)

    _send(
        stdio_server,
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "totally/not/a/real/method",
        },
    )

    # Poll briefly for either a response OR a quiet timeout. The
    # server is allowed to either respond with a JSON-RPC error or
    # to drop the unknown method silently — both prove "did not
    # crash".
    deadline = time.monotonic() + 2.0
    response: Optional[str] = None
    while time.monotonic() < deadline:
        rlist, _, _ = select.select([stdio_server.stdout], [], [], 0.1)
        if rlist:
            response = stdio_server.stdout.readline()  # type: ignore[union-attr]
            break

    if response is not None:
        envelope = json.loads(response)
        # If a response came back, it MUST be a proper JSON-RPC
        # error envelope (error.code + error.message).
        assert (
            "error" in envelope
        ), f"unknown method returned a non-error envelope: {envelope!r}"
        assert "code" in envelope["error"]
        assert "message" in envelope["error"]
    else:
        # No response — the server dropped the bad method. Confirm
        # it's still alive and can serve a subsequent request.
        assert (
            stdio_server.poll() is None
        ), f"server exited after unknown method: rc={stdio_server.returncode}"

    # Either way, a subsequent tools/list must succeed.
    tools = _stdio_list_tools(stdio_server)
    assert len(tools) == 4


# ---------------------------------------------------------------------
# 8. initialize without protocolVersion — server stays alive
# ---------------------------------------------------------------------


def test_stdio_server_initialization_missing_protocol_version(
    stdio_server: subprocess.Popen[str],
) -> None:
    """``initialize`` without ``protocolVersion`` must not crash the server.

    FastMCP 1.x currently returns a JSON-RPC error with
    ``code: -32602`` (Invalid params). This test captures that
    behaviour and additionally asserts the server stays alive for
    a subsequent well-formed handshake — proving the error path
    doesn't leak into the post-handshake state machine.
    """
    _send(
        stdio_server,
        {
            "jsonrpc": "2.0",
            "id": 50,
            "method": "initialize",
            "params": {"clientInfo": {"name": "test"}},
        },
    )
    resp = _recv_json(stdio_server, expected_id=50)

    # Two acceptable shapes:
    #   1. JSON-RPC error envelope (FastMCP 1.x current behaviour)
    #   2. A successful result with a server-chosen default protocol
    #      (if a future SDK release chooses to be lenient).
    if "error" in resp:
        assert isinstance(resp["error"].get("code"), int)
        assert isinstance(resp["error"].get("message"), str)
    else:
        assert "result" in resp, f"unexpected initialize response: {resp!r}"

    # The server MUST still be alive and able to serve a fresh
    # handshake — this is the contract that matters for clients
    # that retry after a bad initial request.
    assert stdio_server.poll() is None, (
        f"server exited after missing protocolVersion: " f"rc={stdio_server.returncode}"
    )

    _stdio_handshake(stdio_server)
    tools = _stdio_list_tools(stdio_server)
    assert len(tools) == 4
