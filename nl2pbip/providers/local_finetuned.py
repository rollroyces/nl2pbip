"""Local provider that targets an OpenAI-compatible endpoint (Ollama or vLLM)."""

from __future__ import annotations

import os
from typing import Dict, List, Optional

import requests


class LocalFineTunedProvider:
    """Minimal client that sends ChatML prompts to a local inference endpoint."""

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.base_url = (
            base_url
            or os.getenv("NL2PBIP_LOCAL_LLM_URL")
            or "http://localhost:11434/v1"
        ).rstrip("/")
        self._session = requests.Session()

    def generate(self, messages: List[Dict[str, str]]) -> str:
        endpoint = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        response = self._session.post(endpoint, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return self._extract_text(data)

    def close(self) -> None:
        self._session.close()

    def _extract_text(self, response_payload: Dict) -> str:
        choices = response_payload.get("choices")
        if not choices:
            raise ValueError("Local endpoint returned no choices.")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not content:
            raise ValueError("Local endpoint returned empty content.")
        return content

    def __enter__(
        self,
    ) -> "LocalFineTunedProvider":  # pragma: no cover - context helper
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - context helper
        self.close()
