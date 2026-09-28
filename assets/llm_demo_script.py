#!/usr/bin/env python3
"""LLM-driven end-to-end demo using nl2pbip's Python API.

Drives a tiny ``StructuredLLMClient``-compatible mock that returns a
canned plan verbatim, so the recording shows the LLM-in-the-loop path
without burning API calls or leaking keys. The orchestrator's
``run_with_reflection`` path is exercised end-to-end (initializer →
planner loop → execute → finalise), which is what `nl2pbip.cli`
internally drives via ``--provider openai`` + ``OPENAI_API_KEY``.

Swap the ``StaticPlanLLM`` for ``StructuredLLMClient`` to use a real
provider; the rest of the script is unchanged.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from nl2pbip.example_run import build_sample_plan
from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StaticPlanLLM:
    """Deterministic LLM stub — returns the canned plan verbatim.

    Implements the ``LLMClient`` protocol from nl2pbip.orchestrator:
    a single ``generate(messages) -> str`` method that returns the
    JSON-encoded plan. ``provider`` / ``model`` are set as class
    attributes for telemetry tags but the orchestrator reads them
    defensively (R-N-10).
    """

    provider = "stub"
    model = "gpt-4o-mini-mock"

    def __init__(self, plan):
        self._plan = plan

    def generate(self, messages):
        return json.dumps(self._plan, indent=2)


def main() -> int:
    artifact_dir = PROJECT_ROOT / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    plan = build_sample_plan(artifact_dir)

    model_path = artifact_dir / "llm_workspace" / "model.tmdl"
    report_path = artifact_dir / "llm_workspace" / "report_workspace.json"
    output_path = Path(plan[-1]["args"]["output_path"])
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Wipe any prior run state so the demo is idempotent.
    for stale in (model_path, report_path, output_path):
        if stale.is_file():
            stale.unlink()
        if stale.is_dir():
            shutil.rmtree(stale)

    orchestrator = Orchestrator(llm_client=StaticPlanLLM(plan))
    register_builtin_tools(orchestrator)

    context = {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
    }

    run_id = uuid.uuid4().hex[:8]
    print(f"# nl2pbip — LLM-driven demo (run_id: {run_id})")
    print(f"# workspace:  {model_path.parent}")
    print(f"# output:     {output_path}")
    print(f"# provider:   {StaticPlanLLM.provider}")
    print(f"# model:      {StaticPlanLLM.model}")
    print()
    time.sleep(1.2)

    prompt = "Customer satisfaction dashboard by region with YoY trend"
    print(f"# prompt: {prompt!r}")
    print("# invoking orchestrator.run_with_reflection(…)")
    print()
    time.sleep(0.8)

    trace = orchestrator.run_with_reflection(user_prompt=prompt, context=context)
    succeeded = trace.succeeded

    time.sleep(0.6)
    print(f"trace.attempts={len(trace.attempts)}")
    print(f"trace.succeeded={succeeded}")
    if not succeeded:
        print(f"trace.final_error={trace.final_error}")

    if succeeded:
        print()
        time.sleep(0.4)
        print("# final artifact paths:")
        print(f"semantic model:  {model_path}  ({model_path.stat().st_size} bytes)")
        print(f"report surface:  {report_path}  ({report_path.stat().st_size} bytes)")
        file_count = sum(1 for _ in output_path.rglob("*") if _.is_file())
        print(f"PBIP package:    {output_path}  ({file_count} files)")
        time.sleep(1.4)

    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
