"""Token / cost budget tracking for LLM calls.

The :class:`TokenBudget` dataclass records per-call and
cumulative spend across an orchestrator run. Callers plumb a
``max_cost_usd`` budget into :class:`Orchestrator`, which calls
:meth:`TokenBudget.check_and_record` after each LLM invocation.
If cumulative spend crosses the budget, the budget raises
:class:`BudgetExceededError` so the orchestrator's retry loop
short-circuits before consuming another attempt.

Token counting uses ``tiktoken`` (the OpenAI tokenizer) when
available and falls back to a deterministic character-based
heuristic (4 chars per token) when ``tiktoken`` is missing — this
keeps the budget layer usable in environments where the optional
``tiktoken`` package hasn't been installed (e.g. slim CI builds).

Cost-per-token is sourced from :mod:`nl2pbip.pricing`, which
carries hardcoded tables for the providers that ship with
:class:`nl2pbip.llm_client.StructuredLLMClient` plus a fallback
of ``$0.000003 USD / token`` for anything unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from nl2pbip.pricing import price_usd_per_token

if TYPE_CHECKING:  # pragma: no cover - typing only
    import tiktoken
else:
    try:  # Optional dependency
        import tiktoken
    except ImportError:  # pragma: no cover - optional import
        tiktoken = None  # type: ignore[assignment]


# Conservative default budget. ``Orchestrator(max_cost_usd=...)`` lets
# callers raise or lower this; the CLI defaults to ``$10`` which
# is roughly 7,000 mid-complexity reports at gpt-4o-mini pricing.
DEFAULT_MAX_COST_USD: float = 10.0


class BudgetExceededError(Exception):
    """Raised when cumulative LLM spend crosses the configured budget.

    The exception carries ``budget`` and ``spent_usd`` attributes so
    callers can render a useful diagnostic ("$5.42 of $5.00 budget
    exhausted after 12 LLM calls") without re-querying the tracker.
    """

    def __init__(
        self,
        message: str,
        *,
        budget_usd: float,
        spent_usd: float,
    ) -> None:
        super().__init__(message)
        self.budget = budget_usd
        self.spent_usd = spent_usd

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"{super().__str__()} "
            f"(spent ${self.spent_usd:.6f} of ${self.budget:.6f} budget)"
        )


@dataclass
class CallRecord:
    """One LLM-call's worth of cost accounting.

    Persisted on :class:`TokenBudget.calls` so callers can audit
    how the budget was consumed.
    """

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float


@dataclass
class TokenBudget:
    """Track per-call + cumulative spend against a USD cap.

    Parameters
    ----------
    max_cost_usd
        Hard cap on cumulative spend. ``DEFAULT_MAX_COST_USD``
        (``$10``) is used when the caller passes ``None``.
    provider
        Default provider name used for cost lookup when the caller
        doesn't supply a per-call provider (e.g. when constructing
        the budget before the LLM client exists).
    model
        Default model name used for cost lookup when the caller
        doesn't supply a per-call model.

    Notes
    -----
    The budget is *strict* — :meth:`check_and_record` raises
    :class:`BudgetExceededError` as soon as the new cumulative
    total would cross ``max_cost_usd``. Callers that want to
    pre-check without recording can use :meth:`would_exceed` or
    :meth:`remaining_usd`.
    """

    max_cost_usd: float = DEFAULT_MAX_COST_USD
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    calls: List[CallRecord] = field(default_factory=list)
    spent_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.max_cost_usd < 0:
            raise ValueError(f"max_cost_usd must be >= 0, got {self.max_cost_usd!r}")

    @property
    def total_tokens(self) -> int:
        """Sum of ``total_tokens`` across every call in ``self.calls``."""
        return sum(call.total_tokens for call in self.calls)

    @property
    def remaining_usd(self) -> float:
        """USD headroom before :class:`BudgetExceededError` fires."""
        return max(0.0, self.max_cost_usd - self.spent_usd)

    def would_exceed(self, additional_cost_usd: float) -> bool:
        """Return ``True`` if adding ``additional_cost_usd`` would cross the cap."""
        return (self.spent_usd + additional_cost_usd) > self.max_cost_usd

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Compute USD cost for ``prompt_tokens`` + ``completion_tokens``.

        Uses :func:`nl2pbip.pricing.price_usd_per_token` to look up
        the per-token rate. Completion tokens are charged at 4x
        the input rate (the typical OpenAI / Anthropic ratio for
        mid-tier models) — a deliberate approximation so the
        budget doesn't underestimate output-heavy plans.
        """
        input_rate = price_usd_per_token(self.provider, self.model)
        output_rate = input_rate * 4.0
        return prompt_tokens * input_rate + completion_tokens * output_rate

    def record_call(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> CallRecord:
        """Record a single LLM call's spend.

        Mutates ``self.calls`` and ``self.spent_usd``. Returns the
        new :class:`CallRecord` for callers that want to attach
        it to a trace.
        """
        effective_provider = provider or self.provider
        effective_model = model or self.model
        # Recompute the rate against the per-call provider/model
        # if it differs from the budget defaults.
        if effective_provider != self.provider or effective_model != self.model:
            input_rate = price_usd_per_token(effective_provider, effective_model)
            output_rate = input_rate * 4.0
            cost = prompt_tokens * input_rate + completion_tokens * output_rate
        else:
            cost = self.estimate_cost(prompt_tokens, completion_tokens)
        record = CallRecord(
            provider=effective_provider,
            model=effective_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost,
        )
        self.calls.append(record)
        self.spent_usd += cost
        return record

    def check_and_record(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> CallRecord:
        """Record a call **and** abort if it pushes us over budget.

        Unlike :meth:`record_call`, this method raises
        :class:`BudgetExceededError` *before* recording when the
        new cumulative total would cross ``max_cost_usd``. The
        spend that triggered the abort is NOT counted against the
        budget — a failed call shouldn't consume headroom.

        Returns the new :class:`CallRecord` when the call fits.
        """
        effective_provider = provider or self.provider
        effective_model = model or self.model
        if effective_provider != self.provider or effective_model != self.model:
            input_rate = price_usd_per_token(effective_provider, effective_model)
            cost = prompt_tokens * input_rate + completion_tokens * (input_rate * 4.0)
        else:
            cost = self.estimate_cost(prompt_tokens, completion_tokens)
        if self.would_exceed(cost):
            raise BudgetExceededError(
                f"LLM cost ${self.spent_usd + cost:.6f} would exceed "
                f"budget ${self.max_cost_usd:.6f}",
                budget_usd=self.max_cost_usd,
                spent_usd=self.spent_usd,
            )
        return self.record_call(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            provider=effective_provider,
            model=effective_model,
        )


# ----------------------------------------------------------------------
# Token counting
# ----------------------------------------------------------------------
_ENCODER: Optional["tiktoken.Encoding"] = None


def _get_encoder() -> Optional["tiktoken.Encoding"]:
    """Return a cached ``tiktoken`` encoder, or ``None`` if unavailable.

    We use ``cl100k_base`` (the GPT-4 / GPT-3.5-turbo tokenizer)
    because it has the widest installed base. For non-OpenAI
    providers this is an approximation; the pricing tables in
    :mod:`nl2pbip.pricing` already account for provider-specific
    rate differences so a slightly-off token count is the
    worst-case downside.
    """
    global _ENCODER
    if _ENCODER is None and tiktoken is not None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def count_tokens(messages: Any, completion_text: str = "") -> Dict[str, int]:
    """Return ``{"prompt_tokens", "completion_tokens"}`` for a chat call.

    ``messages`` follows the OpenAI chat shape (``[{"role":
    "system" | "user" | "assistant", "content": str}, ...]``).
    Non-list inputs are treated as a single user message so
    callers can pass either the message list or a plain string.

    When ``tiktoken`` is missing, falls back to a deterministic
    character-based heuristic (``len(text) // 4``) so the budget
    layer still functions in slim installs. The heuristic
    intentionally *under*-estimates so callers can't trip the
    budget on a tokenizer mismatch.
    """
    encoder = _get_encoder()
    if encoder is not None:
        return _count_tokens_with_encoder(encoder, messages, completion_text)
    return _count_tokens_heuristic(messages, completion_text)


def _count_tokens_with_encoder(
    encoder: "tiktoken.Encoding",
    messages: Any,
    completion_text: str,
) -> Dict[str, int]:
    """Tokenize via ``tiktoken``.

    Uses OpenAI's official per-message overhead (3 tokens per
    message + 1 per ``name`` field) so the count matches what
    OpenAI's billing meter reports.
    """
    if isinstance(messages, str):
        tokens_per_message = 0
    else:
        tokens_per_message = 3
    prompt_tokens = 0
    if isinstance(messages, str):
        prompt_tokens = len(encoder.encode(messages))
    elif isinstance(messages, list):
        for message in messages:
            prompt_tokens += tokens_per_message
            content = ""
            if isinstance(message, dict):
                content = message.get("content", "") or ""
                if message.get("name"):
                    prompt_tokens += 1
            elif isinstance(message, str):
                content = message
            prompt_tokens += len(encoder.encode(content))
    else:
        # Unknown shape — encode the repr as a defensive fallback.
        prompt_tokens = len(encoder.encode(str(messages)))
    completion_tokens = len(encoder.encode(completion_text))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def _count_tokens_heuristic(messages: Any, completion_text: str) -> Dict[str, int]:
    """Fallback token counter (4 chars per token).

    Used when ``tiktoken`` isn't installed. Deliberately
    conservative — we round down so a missing tokenizer never
    causes a budget trip that wouldn't have happened with the
    real encoder.
    """
    prompt_chars = 0
    if isinstance(messages, str):
        prompt_chars = len(messages)
    elif isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                prompt_chars += len(message.get("content", "") or "")
            elif isinstance(message, str):
                prompt_chars += len(message)
    else:
        prompt_chars = len(str(messages))
    return {
        "prompt_tokens": prompt_chars // 4,
        "completion_tokens": len(completion_text) // 4,
    }
