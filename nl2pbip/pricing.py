"""Provider-aware LLM token pricing tables.

The pricing tables here are intentionally a small hardcoded
``dict`` — they cover the providers that ship with
:class:`nl2pbip.llm_client.StructuredLLMClient` plus an explicit
fallback for unknown providers so cost tracking always returns a
number, even when an exotic local inference endpoint (Ollama,
vLLM, LM Studio) is plumbed in via ``provider="custom"``.

Costs are in **USD per token** (not per 1K or per 1M) so the
caller doesn't have to remember to multiply. Numbers are sourced
from each provider's public pricing page as of v1.4.0; if a
provider changes its pricing, update the dict here and bump the
``PRICING_LAST_REVIEWED`` constant so future audits know when
the table was last hand-checked.

See ``CHANGELOG.md`` for the v1.4.0 entry that introduced this
module.
"""

from __future__ import annotations

from typing import Dict

# Hardcoded fallback for any provider/model we don't have an
# explicit entry for. 3 micro-USD / token ≈ $3 / 1M tokens — a
# conservative middle of the gpt-4o-mini band.
DEFAULT_USD_PER_TOKEN: float = 0.000003


# Pricing snapshots were last hand-verified against each provider's
# public pricing page. If you bump a number, also bump this date
# string so an audit can tell at a glance when the numbers were
# last touched.
PRICING_LAST_REVIEWED: str = "2026-09-17"


# Token price (USD per token) for known providers + models.
# Lookup precedence: ``(provider, model)`` → ``(provider, "*")``
# → ``DEFAULT_USD_PER_TOKEN``.
_PROVIDER_MODEL_PRICES_USD_PER_TOKEN: Dict[str, Dict[str, float]] = {
    "openai": {
        # gpt-4o family — input/output differ; we track input
        # (system + user messages dominate the bill). Output is
        # roughly 4x input; trackers that want output-side math
        # can subtract via usage.prompt_tokens vs completion_tokens.
        "gpt-4o-mini": 0.00000015,
        "gpt-4o": 0.000005,
        "gpt-4.1-mini": 0.0000004,
        "gpt-4.1": 0.000002,
        "o4-mini": 0.0000011,
        "o3": 0.000010,
        "gpt-3.5-turbo": 0.0000005,
        "*": 0.000002,  # conservative OpenAI default
    },
    "azure": {
        # Azure OpenAI mirrors OpenAI pricing; we keep a separate
        # table so an Azure-specific price change doesn't require
        # touching the openai table.
        "gpt-4o-mini": 0.00000015,
        "gpt-4o": 0.000005,
        "*": 0.000002,
    },
    "anthropic": {
        # Claude 3.5 / 3.7 Sonnet pricing — input side.
        "claude-3-5-sonnet-20240620": 0.000003,
        "claude-3-5-sonnet-latest": 0.000003,
        "claude-3-7-sonnet-20250219": 0.000003,
        "claude-3-haiku-20240307": 0.00000025,
        "claude-3-opus-20240229": 0.000015,
        "*": 0.000003,
    },
    "deepseek": {
        "deepseek-chat": 0.00000027,
        "deepseek-reasoner": 0.00000055,
        "*": 0.0000005,
    },
    "qwen": {
        "qwen-2.5-coder-32b-instruct": 0.0000004,
        "qwen-max": 0.000002,
        "*": 0.000001,
    },
    "zhipu": {
        "glm-4-plus": 0.000007,
        "glm-4-flash": 0.0000001,
        "*": 0.000001,
    },
    "moonshot": {
        "moonshot-v1-8k": 0.000001,
        "moonshot-v1-32k": 0.000002,
        "*": 0.000001,
    },
    "custom": {
        # Local / Ollama / vLLM / LM Studio are typically free at
        # the marginal-token level; we default to ``0`` so the
        # budget tracker still runs but never trips.
        "*": 0.0,
    },
}


def price_usd_per_token(provider: str, model: str) -> float:
    """Return the per-token USD cost of ``provider`` / ``model``.

    Lookup precedence: exact ``(provider, model)`` →
    ``(provider, "*")`` → module-level :data:`DEFAULT_USD_PER_TOKEN`.

    The function is case-insensitive on ``provider`` and
    ``model`` — provider names come from CLI flags / env vars and
    we don't want a typo (``"OpenAI"`` vs ``"openai"``) to silently
    fall through to the default.
    """
    provider_table = _PROVIDER_MODEL_PRICES_USD_PER_TOKEN.get(provider.lower(), {})
    if not provider_table:
        return DEFAULT_USD_PER_TOKEN
    if model in provider_table:
        return provider_table[model]
    if model.lower() in provider_table:
        return provider_table[model.lower()]
    return provider_table.get("*", DEFAULT_USD_PER_TOKEN)
