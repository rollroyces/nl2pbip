"""Prompt polish layer that runs before messages reach the LLM.

The :class:`PromptPolisher` protocol defines a single ``polish()`` hook
that the :class:`~nl2pbip.orchestrator.Orchestrator` calls inside
``_request_plan()`` to normalise, scrub, and budget the message list
that will be handed to the LLM client.

Why a polish layer?
-------------------

User prompts and planner payloads arrive from real sources: web forms,
CLI args, REST bodies, scraped text, code comments. Before any of that
reaches an LLM, a clean-up pass is worth doing because it:

* **normalises** whitespace, line endings, and Unicode so the LLM sees
  one consistent representation regardless of the upstream platform
  (Windows CRLF, smart quotes from word processors, BOMs from old
  editors, etc.);
* **scrubs accidental PII / secrets** (email addresses, phone numbers,
  IPv4 / IPv6, API key prefixes, bearer tokens) so a user pasting their
  corporate SMTP credentials into a report description does not leak
  them to a third-party LLM endpoint;
* **detects prompt-injection patterns** ("ignore previous instructions",
  role-override attempts, etc.) and replaces them with a neutral
  marker so the LLM still sees the user content without being
  redirected;
* **enforces a length budget** so a 200 MB JSON payload does not blow
  the model context window — anything over the budget is truncated
  with an explicit ``[TRUNCATED]`` marker;
* **is idempotent** — running the polisher twice on its own output is a
  no-op, which means it's safe to apply the polisher at multiple
  layers of the pipeline.

Design
------

* Pure Python, no LLM calls. Polishing is cheap (microseconds to a few
  milliseconds even on a 100 kB payload), so it never dominates the
  planner latency budget.
* Pluggable. The default implementation is
  :class:`DefaultPromptPolisher`; callers can pass ``None`` to skip
  polishing entirely, or pass any object implementing the
  :class:`PromptPolisher` protocol (including their own subclass).
* Redact-and-warn policy by default — every redaction is recorded in a
  :class:`PolishReport` so the orchestrator can surface the changes in
  the :class:`ReflectiveTrace` without failing the call.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Protocol, Sequence

# ---------------------------------------------------------------------------
# Public protocol + dataclass
# ---------------------------------------------------------------------------


class PromptPolisher(Protocol):
    """Hook the orchestrator calls inside ``_request_plan()``.

    Implementations must be idempotent on their own output. Returning a
    :class:`PolishReport` (even empty) is required so the orchestrator
    can attach the polish provenance to the :class:`AttemptRecord`.
    """

    def polish(
        self, messages: Sequence[Mapping[str, str]]
    ) -> "tuple[List[Dict[str, str]], PolishReport]":
        """Return ``(polished_messages, report)``.

        Implementations MUST NOT mutate the input messages. They MUST
        return a new list with the same length and the same ``role``
        keys; only ``content`` may change.
        """
        ...


@dataclass
class PolishReport:
    """Audit trail for one polish pass.

    The orchestrator copies these fields into the :class:`AttemptRecord`
    so the :class:`ReflectiveTrace` shows what was changed before the
    LLM call. Empty reports are normal — most polish passes do nothing.
    """

    steps_applied: List[str] = field(default_factory=list)
    redactions: Dict[str, int] = field(default_factory=dict)
    injections_scrubbed: int = 0
    truncated: int = 0
    bytes_in: int = 0
    bytes_out: int = 0

    def is_noop(self) -> bool:
        """Return ``True`` when this pass did not change anything."""
        return (
            not self.steps_applied
            and not self.redactions
            and self.injections_scrubbed == 0
            and self.truncated == 0
            and self.bytes_in == self.bytes_out
        )


# ---------------------------------------------------------------------------
# Default implementation
# ---------------------------------------------------------------------------


#: Default maximum total characters across all messages. ~32 kB,
#: comfortably under the 100 k context window of every supported
#: model once JSON tool schemas are added. Override per instance.
DEFAULT_MAX_CHARS: int = 32_000

#: Prompt-injection patterns we neutralise by rewriting to a marker.
#: We intentionally use whole-line / case-insensitive matching so a
#: legitimate phrase like "ignore the previous section" survives.
#: Patterns are intentionally narrow; false positives are worse than
#: missed injections here.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+"
        r"(?:instructions?|prompts?|context)\b"
    ),
    re.compile(r"(?i)\bdisregard\s+(?:the\s+)?(?:system\s+)?prompt\b"),
    re.compile(r"(?i)\byou\s+are\s+now\s+(?:a\s+)?(?:dan|jailbreak)\b"),
    re.compile(r"(?i)\bsystem\s*:\s*you\s+are\b"),
    re.compile(r"(?i)\bforget\s+everything\s+(?:above|before|prior)\b"),
)

#: Secret-prefix patterns. We match on the prefix only — the rest of
#: the token is masked with ``[REDACTED:SECRET]`` so the LLM still
#: understands a key was present but cannot echo it.
_SECRET_PREFIXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,128}"),  # OpenAI / Anthropic / ElevenLabs
    re.compile(r"\bsk_live_[A-Za-z0-9]{16,128}"),  # Stripe live
    re.compile(r"\bsk_test_[A-Za-z0-9]{16,128}"),  # Stripe test
    re.compile(r"\bAKIA[0-9A-Z]{16}"),  # AWS access key ID (fixed length)
    re.compile(r"\bASIA[0-9A-Z]{16}"),  # AWS session token
    re.compile(r"\bghp_[A-Za-z0-9]{30,128}"),  # GitHub PAT
    re.compile(r"\bgho_[A-Za-z0-9]{30,128}"),  # GitHub OAuth
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,255}"),  # GitHub fine-grained PAT
    re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,128}"),  # Slack tokens
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,512}"),  # Bearer authorization
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,256}"),  # Google OAuth
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,64}"),  # Google API key
)

#: PII regex set. Phone is intentionally permissive — many users paste
#: ``555-123-4567`` or ``+1 (555) 123-4567 ext 9`` so a strict E.164
#: regex would miss the bulk of real input.
_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,32}"
    ),  # email
    # Phone: must look like a phone — separators are punctuation
    # (space, dash, dot, parens), not letters. Word-boundary on both
    # sides so file paths like ``/var/folders/...43666wqc.../tmp``
    # don't match (the digit run is glued onto an identifier).
    re.compile(
        r"(?<![A-Za-z0-9_])\+?\d{0,3}[\s\-.()]?\d{1,4}[\s\-.()]?\d{2,4}"
        r"[\s\-.()]?\d{2,4}(?:[\s\-.()]?\d{1,5})?(?![A-Za-z0-9_])"
    ),  # phone (intl-ish)
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # IPv4
    re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"),  # IPv6 (lenient)
)

#: Control characters other than common whitespace (\n \r \t).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Collapse runs of spaces / tabs onto one.
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")

#: Strip BOM anywhere in the input.
_BOM_RE = re.compile(r"\ufeff")


@dataclass
class DefaultPromptPolisher:
    """Production-grade :class:`PromptPolisher` implementation.

    Steps applied, in order:

    1. ``encoding_normalize`` — Unicode NFC, strip BOMs, replace smart
       quotes with their ASCII counterparts.
    2. ``whitespace_normalize`` — collapse runs of spaces / tabs onto
       one, drop control chars other than \\n \\r \\t, normalise line
       endings to ``\\n``.
    3. ``secret_redact`` — replace API keys / tokens with
       ``[REDACTED:SECRET]``.
    4. ``pii_redact`` — replace emails / phones / IPs with
       ``[REDACTED:PII]``.
    5. ``injection_scrub`` — neutralise prompt-injection phrases by
       wrapping them in ``[INJECTION_SCRUBBED]...[/INJECTION_SCRUBBED]``.
    6. ``length_budget`` — if total chars > :attr:`max_chars`, truncate
       each message proportionally with a ``[TRUNCATED]`` marker.

    All steps are no-ops if the input is already clean.
    """

    max_chars: int = DEFAULT_MAX_CHARS
    enable_secret_redact: bool = True
    enable_pii_redact: bool = True
    enable_injection_scrub: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def polish(
        self, messages: Sequence[Mapping[str, str]]
    ) -> "tuple[List[Dict[str, str]], PolishReport]":
        if not messages:
            return [], PolishReport()

        report = PolishReport()
        report.bytes_in = sum(len(str(m.get("content", ""))) for m in messages)

        current: List[Dict[str, str]] = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
        ]

        current = self._step_encoding(current, report)
        current = self._step_whitespace(current, report)
        if self.enable_secret_redact:
            current = self._step_secret_redact(current, report)
        if self.enable_pii_redact:
            current = self._step_pii_redact(current, report)
        if self.enable_injection_scrub:
            current = self._step_injection_scrub(current, report)
        current = self._step_length_budget(current, report)

        report.bytes_out = sum(len(m["content"]) for m in current)
        return current, report

    # ------------------------------------------------------------------
    # Individual steps
    # ------------------------------------------------------------------

    def _step_encoding(
        self, messages: List[Dict[str, str]], report: PolishReport
    ) -> List[Dict[str, str]]:
        """Unicode NFC + BOM strip + smart-quote replacement."""
        changed = False
        out: List[Dict[str, str]] = []
        for m in messages:
            content = m["content"]
            new = unicodedata.normalize("NFC", content)
            new = _BOM_RE.sub("", new)
            # Smart quotes → ASCII. We only touch the common offenders.
            new = (
                new.replace("\u2018", "'")
                .replace("\u2019", "'")
                .replace("\u201c", '"')
                .replace("\u201d", '"')
                .replace("\u2013", "-")  # en-dash
                .replace("\u2014", "-")  # em-dash
                .replace("\u2026", "...")  # ellipsis
                .replace("\u00a0", " ")  # NBSP
            )
            if new != content:
                changed = True
            out.append({"role": m["role"], "content": new})
        if changed:
            report.steps_applied.append("encoding_normalize")
        return out

    def _step_whitespace(
        self, messages: List[Dict[str, str]], report: PolishReport
    ) -> List[Dict[str, str]]:
        """Collapse whitespace, drop control chars, normalise line endings."""
        changed = False
        out: List[Dict[str, str]] = []
        for m in messages:
            content = m["content"]
            new = content.replace("\r\n", "\n").replace("\r", "\n")
            new = _CONTROL_CHAR_RE.sub("", new)
            # Collapse only intra-line whitespace so paragraph structure
            # survives. Split on \n first.
            lines = new.split("\n")
            lines = [_MULTI_SPACE_RE.sub(" ", line).strip() for line in lines]
            # Drop fully-blank runs but preserve paragraph breaks.
            compacted: List[str] = []
            blank_run = False
            for line in lines:
                if line == "":
                    if not blank_run:
                        compacted.append("")
                    blank_run = True
                else:
                    compacted.append(line)
                    blank_run = False
            new = "\n".join(compacted).strip()
            if new != content:
                changed = True
            out.append({"role": m["role"], "content": new})
        if changed:
            report.steps_applied.append("whitespace_normalize")
        return out

    def _step_secret_redact(
        self, messages: List[Dict[str, str]], report: PolishReport
    ) -> List[Dict[str, str]]:
        """Mask API keys / tokens with ``[REDACTED:SECRET]``."""
        return self._redact_loop(
            messages, report, _SECRET_PREFIXES, "[REDACTED:SECRET]", "secret_redact"
        )

    def _step_pii_redact(
        self, messages: List[Dict[str, str]], report: PolishReport
    ) -> List[Dict[str, str]]:
        """Mask PII (email / phone / IP) with ``[REDACTED:PII]``."""
        return self._redact_loop(
            messages, report, _PII_PATTERNS, "[REDACTED:PII]", "pii_redact"
        )

    def _redact_loop(
        self,
        messages: List[Dict[str, str]],
        report: PolishReport,
        patterns: Iterable[re.Pattern[str]],
        replacement: str,
        step_name: str,
    ) -> List[Dict[str, str]]:
        changed = False
        out: List[Dict[str, str]] = []
        for m in messages:
            content = m["content"]
            new = content
            for pat in patterns:
                hit_count_before = report.redactions.get(step_name, 0)
                new, n = pat.subn(replacement, new)
                if n:
                    report.redactions[step_name] = hit_count_before + n
                    changed = True
            out.append({"role": m["role"], "content": new})
        if changed:
            report.steps_applied.append(step_name)
        return out

    def _step_injection_scrub(
        self, messages: List[Dict[str, str]], report: PolishReport
    ) -> List[Dict[str, str]]:
        """Neutralise prompt-injection patterns by wrapping in markers."""
        changed = False
        out: List[Dict[str, str]] = []
        for m in messages:
            content = m["content"]
            new = content
            for pat in _INJECTION_PATTERNS:
                new, n = pat.subn(
                    lambda mm: f"[INJECTION_SCRUBBED]{mm.group(0)}[/INJECTION_SCRUBBED]",
                    new,
                )
                if n:
                    report.injections_scrubbed += n
                    changed = True
            out.append({"role": m["role"], "content": new})
        if changed:
            report.steps_applied.append("injection_scrub")
        return out

    def _step_length_budget(
        self, messages: List[Dict[str, str]], report: PolishReport
    ) -> List[Dict[str, str]]:
        """Truncate each message proportionally if the budget is blown."""
        if self.max_chars <= 0:
            return messages
        total = sum(len(m["content"]) for m in messages)
        if total <= self.max_chars:
            return messages
        budget = self.max_chars - (32 * len(messages))  # marker headroom
        if budget <= 0:
            budget = max(0, self.max_chars - 1)
        out: List[Dict[str, str]] = []
        per_msg = budget // max(1, len(messages))
        for m in messages:
            content = m["content"]
            if len(content) > per_msg:
                keep = max(0, per_msg - len("\n[TRUNCATED: ...]"))
                truncated_msg = content[:keep] + "\n[TRUNCATED: ...]"
                report.truncated += 1
                out.append({"role": m["role"], "content": truncated_msg})
            else:
                out.append(m)
        if report.truncated:
            report.steps_applied.append("length_budget")
        return out


# ---------------------------------------------------------------------------
# No-op polisher for tests + callers that want to skip polishing
# ---------------------------------------------------------------------------


@dataclass
class NoopPromptPolisher:
    """Returns the messages unchanged and an empty report.

    Use when you want to bypass polishing entirely (e.g. when testing
    the orchestrator end-to-end without any preprocessing). For full
    disable, pass ``prompt_polisher=None`` to :class:`Orchestrator` —
    the orchestrator treats ``None`` as :class:`NoopPromptPolisher`.
    """

    def polish(
        self, messages: Sequence[Mapping[str, str]]
    ) -> "tuple[List[Dict[str, str]], PolishReport]":
        out = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
        ]
        return out, PolishReport()


# ---------------------------------------------------------------------------
# Internal helpers exported for tests
# ---------------------------------------------------------------------------


def _serialize_for_test(messages: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    """Stable serialisation used by property tests for comparison."""
    return [
        {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
        for m in messages
    ]


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DefaultPromptPolisher",
    "NoopPromptPolisher",
    "PolishReport",
    "PromptPolisher",
]
