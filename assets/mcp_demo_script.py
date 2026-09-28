#!/usr/bin/env python3
"""MCP server stdio exchange demo.

Spawns the ``nl2pbip-mcp`` console script over stdio, then issues
the JSON-RPC handshake + ``tools/list`` + two zero-LLM tool calls
(``version`` and ``validate_pbip``). Captures the wire-protocol
traffic so the README can show what an MCP-compatible agent
(Claude Code, Cursor, Copilot CLI, …) actually sees when it
talks to nl2pbip.

No API keys. No network. Just the stdio JSON-RPC envelope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONSOLE_SCRIPT = PROJECT_ROOT / ".venv" / "bin" / "nl2pbip-mcp"
PBIP_DIR = PROJECT_ROOT / "artifacts" / "SalesInsights.pbipdir"


def _send(proc, payload):
    line = json.dumps(payload)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()
    return line


def _recv(proc):
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line)


def _rpc(proc, request_id, method, params=None):
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            **({"params": params} if params is not None else {}),
        },
    )
    return _recv(proc)


def main() -> int:
    # 1. Ensure we have a .pbip dir to validate (the no-LLM demo
    # produces one). If it doesn't exist, build it first.
    if not PBIP_DIR.exists():
        print(f"# {PBIP_DIR} missing — regenerating via example_run")
        rc = subprocess.call(
            [".venv/bin/python", "-m", "nl2pbip.example_run"],
            cwd=PROJECT_ROOT,
        )
        if rc != 0:
            print(f"# example_run failed with exit={rc}", file=sys.stderr)
            return rc

    print("# nl2pbip — MCP stdio exchange demo")
    print(f"# console script: {CONSOLE_SCRIPT}")
    print(f"# validating:     {PBIP_DIR}")
    print()

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONPATH", str(PROJECT_ROOT))

    proc = subprocess.Popen(
        [str(CONSOLE_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )

    try:
        # 2. initialize handshake
        print(">>> initialize")
        resp = _rpc(
            proc,
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nl2pbip-demo", "version": "0.1.0"},
            },
        )
        print(json.dumps(resp, indent=2)[:600])
        print()

        # 3. notifications/initialized (no response expected)
        print(">>> notifications/initialized")
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.1)
        print("(no response — notification)")
        print()

        # 4. tools/list
        print(">>> tools/list")
        resp = _rpc(proc, 2, "tools/list")
        tools = resp.get("result", {}).get("tools", [])
        for t in tools:
            print(f"  • {t['name']:18s}  {t.get('description', '')[:60]}")
        print()

        # 5. tools/call version (zero-LLM)
        print(">>> tools/call  version")
        resp = _rpc(
            proc,
            3,
            "tools/call",
            {"name": "version", "arguments": {}},
        )
        text = resp.get("result", {}).get("content", [{}])[0].get("text", "")
        try:
            parsed = json.loads(text)
            print(json.dumps(parsed, indent=2))
        except json.JSONDecodeError:
            print(text)
        print()

        # 6. tools/call validate_pbip (zero-LLM)
        print(">>> tools/call  validate_pbip")
        resp = _rpc(
            proc,
            4,
            "tools/call",
            {"name": "validate_pbip", "arguments": {"pbip_path": str(PBIP_DIR)}},
        )
        text = resp.get("result", {}).get("content", [{}])[0].get("text", "")
        try:
            parsed = json.loads(text)
            print(json.dumps(parsed, indent=2)[:1200])
        except json.JSONDecodeError:
            print(text)
        print()

        print("# 4 tools exposed, 2 zero-LLM calls (version + validate_pbip).")
        print("# generate_report + inspect_dataset require an LLM provider —")
        print("# see scripts/record_llm_demo.py for the LLM-driven flow.")
        return 0
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
