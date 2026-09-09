"""Provider-agnostic LLM client that emits structured nl2pbip plans."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

try:  # Optional dependency
    from openai import OpenAI
    from openai import AzureOpenAI  # type: ignore
except ImportError:  # pragma: no cover - optional import
    OpenAI = None  # type: ignore
    AzureOpenAI = None  # type: ignore

try:  # Optional dependency
    import anthropic
except ImportError:  # pragma: no cover - optional import
    anthropic = None  # type: ignore


@dataclass(frozen=True)
class ProviderConfig:
    """Static metadata describing how to connect to a provider."""

    name: str
    client: str  # openai, azure, anthropic
    env_keys: Sequence[str]
    default_models: Sequence[str]
    base_url: Optional[str] = None
    endpoint_env_keys: Sequence[str] = ()
    supports_json: bool = True
    default_api_version: Optional[str] = None


_PROVIDER_ALIASES = {
    "azureopenai": "azure",
    "azure-openai": "azure",
}

_PROVIDER_CONFIGS: Dict[str, ProviderConfig] = {
    "openai": ProviderConfig(
        name="openai",
        client="openai",
        env_keys=("OPENAI_API_KEY",),
        default_models=("gpt-4o-mini", "gpt-4o"),
        supports_json=True,
    ),
    "azure": ProviderConfig(
        name="azure",
        client="azure",
        env_keys=("AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"),
        endpoint_env_keys=("AZURE_OPENAI_ENDPOINT",),
        default_models=("gpt-4o-mini", "gpt-4o"),
        supports_json=True,
        default_api_version="2024-06-01",
    ),
    "anthropic": ProviderConfig(
        name="anthropic",
        client="anthropic",
        env_keys=("ANTHROPIC_API_KEY",),
        default_models=("claude-3-5-sonnet-20240620",),
        supports_json=False,
    ),
    "deepseek": ProviderConfig(
        name="deepseek",
        client="openai",
        env_keys=("DEEPSEEK_API_KEY",),
        base_url="https://api.deepseek.com",
        default_models=("deepseek-chat", "deepseek-reasoner"),
        supports_json=True,
    ),
    "qwen": ProviderConfig(
        name="qwen",
        client="openai",
        env_keys=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_models=("qwen-2.5-coder-32b-instruct", "qwen-max"),
        supports_json=True,
    ),
    "zhipu": ProviderConfig(
        name="zhipu",
        client="openai",
        env_keys=("ZHIPU_API_KEY",),
        base_url="https://open.bigmodel.cn/api/paas/v4/",
        default_models=("glm-4-plus", "glm-4-flash"),
        supports_json=True,
    ),
    "moonshot": ProviderConfig(
        name="moonshot",
        client="openai",
        env_keys=("MOONSHOT_API_KEY",),
        base_url="https://api.moonshot.cn/v1",
        default_models=("moonshot-v1-8k", "moonshot-v1-32k"),
        supports_json=True,
    ),
    "custom": ProviderConfig(
        name="custom",
        client="openai",
        env_keys=("NL2PBIP_LLM_API_KEY", "OPENAI_API_KEY"),
        endpoint_env_keys=("NL2PBIP_LLM_BASE_URL",),
        default_models=("gpt-4o-mini",),
        supports_json=True,
    ),
}


class StructuredLLMClient:
    """Concrete ``LLMClient`` that enforces plan JSON output."""

    _SECURITY_GUIDANCE = (
        "When prompts mention sensitive data, row-level filtering, role-based access, object-level hiding,"
        " or phrases like 'hide salary column' or 'restrict access to employee table', plan security roles accordingly."
        " Use add_rls_role for dynamic/static row filters (USERPRINCIPALNAME(), CUSTOMDATA(), dimension attributes)"
        " and add_ols_role to hide entire tables or specific columns via metadataPermission: none so the objects disappear for assigned users."
    )

    _LANGUAGE_GUIDANCE = (
        "Users may describe requirements in either English or Chinese. Generate steps, explanations,"
        " and status messages in the user's language, but ALWAYS emit technical identifiers (table names,"
        " columns, DAX measures, calculation groups, metadata fields) using clear English naming conventions."
    )

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_output_tokens: int = 1500,
        max_retries: int = 3,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> None:
        preferred_provider = provider or os.getenv("NL2PBIP_LLM_PROVIDER") or "openai"
        normalized_provider = self._normalize_provider(preferred_provider)
        self._provider_config = self._get_provider_config(normalized_provider)
        self.provider = normalized_provider
        env_model = os.getenv("NL2PBIP_LLM_MODEL")
        self.model = model or env_model or self._default_model()
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self._base_url_override = base_url
        self._api_key_override = api_key
        self._api_version_override = api_version
        self._openai_client: Optional[OpenAI] = None
        self._anthropic_client: Optional["anthropic.Anthropic"] = None
        self._azure_client: Optional["AzureOpenAI"] = None

    def generate(self, messages: List[Dict[str, str]]) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw_text = self._invoke_provider(messages)
                return self._normalize_plan(raw_text)
            except Exception as exc:  # pragma: no cover - network code
                last_error = exc
                if attempt == self.max_retries:
                    break
                time.sleep(1.5 * attempt)
        raise RuntimeError("LLM client failed after retries.") from last_error

    # ------------------------------------------------------------------
    # Provider dispatch
    # ------------------------------------------------------------------
    def _invoke_provider(self, messages: List[Dict[str, str]]) -> str:
        if self._provider_config.client == "openai":
            return self._call_openai_compatible(messages)
        if self._provider_config.client == "azure":
            return self._call_azure_openai(messages)
        if self._provider_config.client == "anthropic":
            return self._call_anthropic(messages)
        raise ValueError(f"Unsupported LLM provider '{self.provider}'.")

    def _call_openai_compatible(self, messages: List[Dict[str, str]]) -> str:
        if OpenAI is None:
            raise ImportError("openai package is required for provider 'openai'.")
        api_key = self._resolve_api_key()
        if not api_key:
            env_names = ", ".join(self._provider_config.env_keys)
            raise EnvironmentError(
                f"API key must be configured for provider '{self.provider}'. Set {env_names} or pass --api-key."
            )
        base_url = self._resolve_endpoint()
        if self.provider == "custom" and not base_url:
            raise EnvironmentError(
                "Custom provider requires a base URL. Pass --base-url or set NL2PBIP_LLM_BASE_URL."
            )
        if self._openai_client is None:
            client_kwargs: Dict[str, Any] = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            self._openai_client = OpenAI(**client_kwargs)
        request_payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        if self._provider_config.supports_json:
            request_payload["response_format"] = {"type": "json_object"}
        response = self._openai_client.chat.completions.create(**request_payload)
        return response.choices[0].message.content or ""

    def _call_azure_openai(self, messages: List[Dict[str, str]]) -> str:
        if AzureOpenAI is None:
            raise ImportError("openai package >=1.18 is required for Azure OpenAI support.")
        api_key = self._resolve_api_key()
        if not api_key:
            env_names = ", ".join(self._provider_config.env_keys)
            raise EnvironmentError(
                f"Azure OpenAI requires an API key. Set {env_names} or pass --api-key."
            )
        endpoint = self._resolve_endpoint()
        if not endpoint:
            raise EnvironmentError(
                "Azure OpenAI requires an endpoint. Pass --base-url or set AZURE_OPENAI_ENDPOINT."
            )
        api_version = self._resolve_api_version()
        if not api_version:
            raise EnvironmentError(
                "Azure OpenAI requires an API version. Pass --api-version or set AZURE_OPENAI_API_VERSION."
            )
        if self._azure_client is None:
            self._azure_client = AzureOpenAI(
                api_key=api_key,
                azure_endpoint=endpoint,
                api_version=api_version,
            )
        request_payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        if self._provider_config.supports_json:
            request_payload["response_format"] = {"type": "json_object"}
        response = self._azure_client.chat.completions.create(**request_payload)
        return response.choices[0].message.content or ""

    def _call_anthropic(self, messages: List[Dict[str, str]]) -> str:
        if anthropic is None:
            raise ImportError("anthropic package is required for provider 'anthropic'.")
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY must be set for Anthropic provider.")
        if self._anthropic_client is None:
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        system_prompt, chat_messages = self._split_messages(messages)
        response = self._anthropic_client.messages.create(  # pragma: no cover - network code
            model=self.model,
            system=system_prompt,
            messages=self._to_anthropic_messages(chat_messages),
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
        )
        text_parts = [part.text for part in response.content if part.type == "text"]
        return "".join(text_parts)

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------
    def _split_messages(self, messages: List[Dict[str, str]]) -> tuple[str, List[Dict[str, str]]]:
        system_chunks: List[str] = []
        forward: List[Dict[str, str]] = []
        for message in messages:
            if message.get("role") == "system":
                system_chunks.append(message.get("content", ""))
            else:
                forward.append(message)
        base_prompt: List[str] = []
        if system_chunks:
            base_prompt.extend(system_chunks)
        else:
            base_prompt.append("You are a helpful planner.")
        base_prompt.append(self._LANGUAGE_GUIDANCE)
        base_prompt.append(self._SECURITY_GUIDANCE)
        system_prompt = "\n\n".join(chunk for chunk in base_prompt if chunk)
        return system_prompt, forward

    def _to_anthropic_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for message in messages:
            converted.append(
                {
                    "role": message["role"],
                    "content": [{"type": "text", "text": message.get("content", "")}],
                }
            )
        return converted

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------
    def _normalize_plan(self, text: str) -> str:
        if not text:
            raise ValueError("LLM returned empty content.")
        cleaned = text.strip()
        candidates = [cleaned]
        fenced = self._extract_code_fence(cleaned)
        if fenced and fenced not in candidates:
            candidates.append(fenced)
        for candidate in candidates:
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return self._format_plan_payload(payload)
        extracted = self._extract_first_json_object(cleaned)
        if extracted is not None:
            return self._format_plan_payload(extracted)
        raise ValueError("LLM response must contain a 'plan' array.")

    def _format_plan_payload(self, payload: Any) -> str:
        if isinstance(payload, list):
            payload = {"plan": payload}
        if not isinstance(payload, dict) or "plan" not in payload:
            raise ValueError("LLM response must contain a 'plan' array.")
        plan = payload["plan"]
        if not isinstance(plan, list):
            raise ValueError("'plan' must be a list of tool calls.")
        return json.dumps({"plan": plan}, indent=2)

    @staticmethod
    def _extract_code_fence(text: str) -> Optional[str]:
        fence_start = text.find("```")
        if fence_start == -1:
            return None
        remainder = text[fence_start + 3 :]
        newline = remainder.find("\n")
        if newline != -1:
            remainder = remainder[newline + 1 :]
        fence_end = remainder.rfind("```")
        if fence_end != -1:
            remainder = remainder[:fence_end]
        return remainder.strip()

    @staticmethod
    def _extract_first_json_object(text: str) -> Optional[Any]:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char in "[{":
                try:
                    payload, _ = decoder.raw_decode(text[idx:])
                except json.JSONDecodeError:
                    continue
                return payload
        return None

    def _default_model(self) -> str:
        defaults = self._provider_config.default_models
        if defaults:
            return defaults[0]
        return "gpt-4o-mini"

    @classmethod
    def _normalize_provider(cls, provider: str) -> str:
        normalized = (provider or "").lower()
        return _PROVIDER_ALIASES.get(normalized, normalized)

    @classmethod
    def _get_provider_config(cls, provider: str) -> ProviderConfig:
        if provider not in _PROVIDER_CONFIGS:
            supported = ", ".join(sorted(_PROVIDER_CONFIGS))
            raise ValueError(f"Unsupported LLM provider '{provider}'. Supported providers: {supported}.")
        return _PROVIDER_CONFIGS[provider]

    def _resolve_api_key(self) -> Optional[str]:
        if self._api_key_override:
            return self._api_key_override
        return self._lookup_env(self._provider_config.env_keys)

    def _resolve_endpoint(self) -> Optional[str]:
        if self._base_url_override:
            return self._base_url_override
        general_override = os.getenv("NL2PBIP_LLM_BASE_URL")
        if general_override:
            return general_override
        env_endpoint = self._lookup_env(self._provider_config.endpoint_env_keys)
        if env_endpoint:
            return env_endpoint
        return self._provider_config.base_url

    def _resolve_api_version(self) -> Optional[str]:
        return self._api_version_override or os.getenv("AZURE_OPENAI_API_VERSION") or self._provider_config.default_api_version

    @staticmethod
    def _lookup_env(names: Sequence[str]) -> Optional[str]:
        for name in names:
            value = os.getenv(name)
            if value:
                return value
        return None
