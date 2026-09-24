"""Provider-agnostic LLM client that emits structured nl2pbip plans."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, cast

try:  # Optional dependency
    from openai import (
        AzureOpenAI,
        OpenAI,
    )
except ImportError:  # pragma: no cover - optional import
    # When the SDK isn't installed, fall back to ``None`` so
    # attribute lookups fail loudly at runtime instead of
    # producing a silent miscompile. The trailing ignores
    # are needed when the SDK IS installed (mypy rejects
    # ``type[X] = None``); the ``unused-ignore`` ignore
    # suppresses the warning when the SDK is NOT installed
    # and the assignment is unreachable from mypy's view.
    OpenAI = None  # type: ignore[assignment,misc,unused-ignore]
    AzureOpenAI = None  # type: ignore[assignment,misc,unused-ignore]

try:  # Optional dependency
    import anthropic
except ImportError:  # pragma: no cover - optional import
    anthropic = None  # type: ignore[assignment,unused-ignore]


if TYPE_CHECKING:  # pragma: no cover - typing only
    from nl2pbip.budget import TokenBudget

from nl2pbip.budget import count_tokens


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
        budget: Optional["TokenBudget"] = None,
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
        # Optional budget tracker — when attached, every successful
        # ``generate()`` call records its token spend. The
        # orchestrator plumbs a single ``TokenBudget`` into the LLM
        # client at construction time so per-call accounting is
        # automatic; tests can also pass a budget directly to
        # assert specific dollar amounts.
        self._budget: Optional["TokenBudget"] = budget

    def generate(self, messages: List[Dict[str, str]]) -> str:
        """Run the provider call + budget charge + plan normalisation.

        Wrapped in an OTEL ``nl2pbip.llm.chat`` span (when
        telemetry is enabled) carrying ``provider``, ``model``,
        ``tokens_in``, ``tokens_out``, ``cost_usd``, and
        ``budget_remaining_usd``. The span attributes are set
        BEFORE the provider call so a slow LLM still shows
        useful information in the trace while it's in flight.
        """
        # Local import: keep ``opentelemetry`` off the import
        # graph when telemetry is disabled.
        from nl2pbip.telemetry import get_tracer

        tracer = get_tracer(__name__)
        # Pre-count prompt tokens so the span gets a useful
        # ``tokens_in`` attribute even when the LLM itself
        # never reports token usage.
        prompt_estimate = count_tokens(messages, "")
        prompt_in = prompt_estimate.get("prompt_tokens", 0)
        llm_attrs: Dict[str, Any] = {
            "provider": str(self.provider),
            "model": str(self.model),
            "tokens_in": prompt_in,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "budget_remaining_usd": self._budget_remaining_safe(),
        }
        with tracer.start_as_current_span("nl2pbip.llm.chat", attributes=llm_attrs):
            return self._generate_impl(messages)

    def _generate_impl(self, messages: List[Dict[str, str]]) -> str:
        """Body of :meth:`generate`, extracted so the OTEL span
        can wrap it.

        Used to be inlined in ``generate``; behaviour is
        unchanged. The span it runs under is named
        ``nl2pbip.llm.chat`` in :meth:`generate` so the public
        method name does not have to be renamed.
        """
        from nl2pbip.telemetry import get_current_span, record_event

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw_text = self._invoke_provider(messages)
                # Charge the budget AFTER a successful response so a
                # network / parse retry doesn't waste a retry attempt
                # on a budget check. Budget checks themselves raise
                # ``BudgetExceededError`` so callers see a single,
                # consistent error type.
                if self._budget is not None:
                    self._charge_budget(messages, raw_text)
                # Reflect the post-call budget state on the
                # parent span so traces show the running
                # cost and remaining headroom after the LLM
                # answers.
                counts = count_tokens(messages, raw_text)
                span = get_current_span()
                try:
                    span.set_attribute("tokens_in", int(counts.get("prompt_tokens", 0)))
                    span.set_attribute(
                        "tokens_out", int(counts.get("completion_tokens", 0))
                    )
                except Exception:  # pragma: no cover - defensive
                    pass  # nosec B110 — best-effort span attribute setting
                # ``spent_usd`` is the cumulative cost in USD
                # tracked by TokenBudget; reported as the
                # per-call ``cost_usd`` only when the budget is
                # attached. Otherwise record 0 so trace consumers
                # don't see ``None``.
                spent_usd = 0.0
                if self._budget is not None:
                    spent_usd = float(self._budget.spent_usd)
                    # Capture the marginal cost of THIS call by
                    # looking at the last appended CallRecord.
                    calls = self._budget.calls
                    if calls:
                        spent_usd = float(calls[-1].cost_usd)
                try:
                    span.set_attribute("cost_usd", spent_usd)
                    span.set_attribute(
                        "budget_remaining_usd",
                        self._budget_remaining_safe(),
                    )
                except Exception:  # pragma: no cover - defensive
                    pass  # nosec B110 — best-effort span attribute setting
                record_event(
                    "nl2pbip.llm.completed",
                    {
                        # ``count_tokens`` returns a dict with
                        # ``int`` values, so the explicit ``int``
                        # cast here is redundant; left implicit
                        # for mypy when the dict type is widened.
                        "tokens_in": counts.get("prompt_tokens", 0),
                        "tokens_out": counts.get("completion_tokens", 0),
                        "cost_usd": spent_usd,
                    },
                )
                return self._normalize_plan(raw_text)
            except Exception as exc:  # pragma: no cover - network code
                last_error = exc
                if attempt == self.max_retries:
                    break
                time.sleep(1.5 * attempt)
        raise RuntimeError("LLM client failed after retries.") from last_error

    def _budget_remaining_safe(self) -> float:
        """Return the live budget headroom in USD, or ``-1.0`` when no
        budget is attached. Used by the OTEL span attributes.
        """
        if self._budget is None:
            return -1.0
        return float(self._budget.remaining_usd)

    def _charge_budget(
        self, messages: List[Dict[str, str]], completion_text: str
    ) -> None:
        """Record this call's token spend against ``self._budget``.

        Uses :func:`nl2pbip.budget.count_tokens` for tokenization.
        Raises :class:`BudgetExceededError` if the call would
        cross the budget cap; the orchestrator treats this as a
        terminal failure for the current run.
        """
        if self._budget is None:
            return

        counts = count_tokens(messages, completion_text)
        self._budget.check_and_record(
            prompt_tokens=counts["prompt_tokens"],
            completion_tokens=counts["completion_tokens"],
            provider=self.provider,
            model=self.model,
        )

    def set_budget(self, budget: Optional["TokenBudget"]) -> None:
        """Attach / detach a :class:`TokenBudget` after construction.

        The orchestrator's ``_attach_budget_to_llm`` looks for
        this method first so a budget can be wired in even when
        the LLM was instantiated before the budget decision was
        made (CLI parsing, lazy config, etc.). Passing ``None``
        detaches the budget.
        """
        self._budget = budget

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
            raise ImportError(
                "openai package >=1.18 is required for Azure OpenAI support."
            )
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
            raise EnvironmentError(
                "ANTHROPIC_API_KEY must be set for Anthropic provider."
            )
        if self._anthropic_client is None:
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        system_prompt, chat_messages = self._split_messages(messages)
        # Cast: anthropic SDK overloads don't expose temperature as a
        # top-level field on the messages.create() stub, but it is a
        # valid kwarg at runtime. Cast Any so mypy stops complaining.
        anthropic_client = cast(Any, self._anthropic_client)
        response = anthropic_client.messages.create(  # pragma: no cover - network code
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
    def _split_messages(
        self, messages: List[Dict[str, str]]
    ) -> tuple[str, List[Dict[str, str]]]:
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

    def _to_anthropic_messages(
        self, messages: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
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
                # ``raw_decode`` takes the position where to start,
                # which avoids the O(n) substring allocation that
                # ``text[idx:]`` would do at every ``[`` or ``{``.
                try:
                    payload, _ = decoder.raw_decode(text, idx)
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
            raise ValueError(
                f"Unsupported LLM provider '{provider}'. Supported providers: {supported}."
            )
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
        return (
            self._api_version_override
            or os.getenv("AZURE_OPENAI_API_VERSION")
            or self._provider_config.default_api_version
        )

    @staticmethod
    def _lookup_env(names: Sequence[str]) -> Optional[str]:
        for name in names:
            value = os.getenv(name)
            if value:
                return value
        return None
