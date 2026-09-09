from __future__ import annotations

import json

from nl2pbip.llm_client import StructuredLLMClient


def test_normalize_plan_handles_markdown_fence() -> None:
    client = StructuredLLMClient(provider="openai")
    response = """Here you go:\n```json\n{\n  \"plan\": [{\"tool\": \"package_pbip\", \"args\": {}}]\n}\n```"""

    normalized = client._normalize_plan(response)
    payload = json.loads(normalized)

    assert payload["plan"][0]["tool"] == "package_pbip"


def test_normalize_plan_extracts_first_json_object() -> None:
    client = StructuredLLMClient(provider="openai")
    response = 'Plan summary before JSON.\n{\n  "plan": [{"tool": "add_report_page", "args": {"page": "Main"}}]\n}\nThanks!'

    normalized = client._normalize_plan(response)
    payload = json.loads(normalized)

    assert payload["plan"][0]["tool"] == "add_report_page"
