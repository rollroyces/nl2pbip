# pyright: reportMissingImports=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
"""Shared pytest fixtures and helpers for the nl2pbip test suite.

Consolidates utilities that were previously duplicated across 5-7 test
modules:

- ``REPO_ROOT`` — absolute path to the project root, used by every
  test that needs to reach ``pyproject.toml``, ``.github/``,
  ``scripts/``, ``vendor/``, etc.
- ``tomllib_load`` — Python 3.10-compatible wrapper around
  ``tomllib.load`` (falls back to ``tomli`` on 3.10).
- ``stub_llm_client`` — factory that returns a ``MagicMock`` matching
  the ``LLMClient`` protocol. Useful for tests that want to assert
  on call counts / return values without spinning up a real LLM.
- ``minimal_plan`` — pytest fixture returning the 5-step plan that
  produces a packaged ``.pbipdir`` (page → table → measure → visual
  → package). Duplicated verbatim in ``test_mcp_server_v160.py`` and
  ``test_mcp_server_stdio.py`` before this consolidation.
- ``MCP_HEADERS`` — the ``Accept`` + ``Content-Type`` header pair the
  streamable-http transport requires on every RPC. Used by the MCP
  client helpers below.
- ``parse_sse_jsonrpc`` — extract a JSON-RPC payload from a
  ``text/event-stream`` body.
- ``wait_for_bind`` — poll a TCP port until the server binds, with a
  timeout. Used by the streamable-http fixture launcher.
- ``_mute_logging`` — autouse fixture that clamps the root logger to
  ``CRITICAL`` so test output isn't drowned in INFO chatter from the
  orchestrator / TMDL handler / providers modules.

Conftest is automatically discovered by pytest; tests get these names
without explicit imports. ``from .conftest import ...`` works too for
modules that want to call helpers directly (e.g. for type checking).
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Union
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------
# Filesystem constants
# ---------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
"""Absolute path to the project root (``/Users/.../nl2pbip``).

Every test that needs ``pyproject.toml`` / ``.github/`` / ``scripts/``
/ ``vendor/`` resolves paths against this constant. The brief
explicitly named this so contributors don't keep re-deriving it via
``Path(__file__).resolve().parent.parent`` in every module.
"""


# ---------------------------------------------------------------------
# tomllib compat (Python 3.10 still ships tomli)
# ---------------------------------------------------------------------

try:  # pragma: no cover — trivial branch
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover — Python 3.10 path
    import tomli as tomllib  # type: ignore[no-redef]


def tomllib_load(path: Union[str, Path]) -> Dict[str, Any]:
    """Read a TOML file into a dict.

    Accepts either a :class:`pathlib.Path` / ``str`` path *or* an
    already-open binary file handle (the stdlib
    :func:`tomllib.load` accepts only the latter). Used by
    ``test_repo_templates.py`` and ``test_custom_visuals.py``.
    """
    # ``BinaryIO`` has a ``.name`` attribute; if we got one, reuse
    # it (the stdlib ``tomllib.load`` only takes a binary file).
    fh_name = getattr(path, "name", None)
    if fh_name is not None:
        with open(fh_name, "rb") as fh:
            return tomllib.load(fh)
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------
# LLM client stub factory
# ---------------------------------------------------------------------


def stub_llm_client(
    plan: Union[Dict[str, Any], str, List[Any], None] = None,
) -> MagicMock:
    """Return a ``MagicMock`` that quacks like :class:`LLMClient`.

    The mock's ``provider`` and ``model`` attributes are set so the
    protocol-required fields satisfy :class:`LLMClient`'s ``Protocol``
    shape (which was promoted to required ``provider`` + ``model`` in
    v2.0.2). ``plan`` defaults to an empty list — pass a dict to
    have ``generate_plan`` return it directly.

    Usage::

        orch = Orchestrator(llm_client=stub_llm_client([{"tool": ...}]))
    """
    mock = MagicMock(name="LLMClient")
    # Protocol-required attrs (post-v2.0.2)
    mock.provider = "stub"
    mock.model = "stub-model-v1"
    mock.generate_plan.return_value = plan if plan is not None else []
    mock.complete.return_value = "stub"
    return mock


# ---------------------------------------------------------------------
# Minimal-plan fixture (5-step plan that packages a .pbipdir)
# ---------------------------------------------------------------------


@pytest.fixture
def minimal_plan(tmp_path: Path) -> List[Dict[str, Any]]:
    """Return the minimum viable plan that produces a packaged ``.pbipdir``.

    Steps: ``add_report_page`` → ``create_table`` → ``add_measure`` →
    ``add_visual`` → ``package_pbip``. Output dir is rooted at the
    per-test ``tmp_path`` so tests can clean up with ``rm -rf``.

    Used by ``test_mcp_server_v160.py``, ``test_mcp_server_stdio.py``,
    ``test_agentic_reflection.py``, ``test_prompts.py``, and the new
    ``test_mcp_server.py`` cases that exercise the artifact path.
    """
    return _build_minimal_plan(tmp_path, "Minimal")


def _build_minimal_plan(tmp_path: Path, project_name: str) -> List[Dict[str, Any]]:
    """Build the 5-step plan that packages a ``.pbipdir`` under ``project_name``.

    Factored out so individual tests can pass their own
    ``project_name`` (some tests want a distinct name to assert the
    packaged output path). Mirrors the previous
    ``_make_minimal_plan`` helper that lived in both
    ``test_mcp_server_v160.py`` and ``test_mcp_server_stdio.py``.
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
# Streamable-HTTP transport helpers
# ---------------------------------------------------------------------

MCP_HEADERS: Dict[str, str] = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
"""Default header pair for streamable-http MCP requests.

The streamable-http transport advertises ``application/json`` for
RPC requests and ``text/event-stream`` for the response. Servers
that strictly enforce this header combination reject calls without
both values.
"""


def parse_sse_jsonrpc(body: str) -> Dict[str, Any]:
    """Pull the JSON-RPC payload out of a streamable-http SSE body.

    The MCP streamable-http transport replies with
    ``text/event-stream`` content. Each event is a ``data:`` line
    followed by a blank line; the JSON-RPC payload lives on the
    ``data:`` line. Some events (notably the
    ``notifications/initialized`` ack the server emits after
    ``initialize``) carry no ``data:`` payload at all, so we skip
    them.

    Raises :class:`AssertionError` if no JSON frame is found.
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


def wait_for_bind(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll ``(host, port)`` until a TCP connection succeeds.

    Returns ``True`` if the port accepts a connection within
    ``timeout`` seconds, ``False`` otherwise. Used by MCP server
    fixtures that launch a uvicorn / starlette app in a subprocess
    or thread — we don't know precisely when the listener is ready.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


def free_port() -> int:
    """Bind port 0 on localhost, read the assigned port, release it.

    The returned port is "best effort" free — another process could
    grab it between this call and the server binding. Callers should
    still pass through ``wait_for_bind`` to confirm.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------
# Logging mute (autouse)
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mute_logging() -> Generator[None, None, None]:
    """Mute the root logger for the duration of each test.

    The orchestrator, TMDL handler, providers, and telemetry modules
    log at INFO during normal operation. Test runs are much easier to
    read without 1k lines of "Loaded PBIP model …" interspersed with
    assertion failures. Set ``CAPLOG=1`` in the env to re-enable.
    """
    if os_environ_truthy("CAPLOG"):
        return  # leave logging alone for tests that need it
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        root.setLevel(original_level)


def os_environ_truthy(name: str) -> bool:
    """Return True iff ``os.environ[name]`` is a truthy string.

    Accepts ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case-insensitive).
    Anything else (including unset) returns False.
    """
    import os

    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}
