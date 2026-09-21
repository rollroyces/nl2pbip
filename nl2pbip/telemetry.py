"""OpenTelemetry tracing facade for nl2pbip.

Exposes a stdlib-only fallback when the OpenTelemetry SDK is not
installed (the default ``telemetry`` extra is *opt-in* — most
users do not want OTEL on the import path). When the SDK is
installed the facade delegates to ``opentelemetry.trace`` so
spans flow to whatever exporter the host application configures
(Console, OTLP/HTTP, Jaeger, Tempo, Honeycomb, etc.).

The module-level ``TRACER_PROVIDER`` is configured once via
``_configure_tracing()``. Re-configuration is a no-op so test
fixtures can call it freely.

Environment variables
---------------------

``NL2PBIP_OTEL_EXPORTER``
    ``none`` (default) | ``console`` | ``otlp_http``.

``NL2PBIP_OTEL_OTLP_ENDPOINT``
    OTLP/HTTP endpoint (only used by ``otlp_http``). Defaults to
    ``http://localhost:4318`` (the Jaeger / Tempo default).

``NL2PBIP_OTEL_SERVICE_NAME``
    ``Resource.service.name``. Defaults to ``nl2pbip``.

``NL2PBIP_OTEL_CONSOLE_OUT``
    Where the console exporter writes. ``stderr`` (default) or
    ``stdout``.

Design notes
------------

* All public helpers (``get_tracer``, ``get_current_span``, etc.)
  return **real** SDK objects when the SDK is importable, and
  **deterministic no-op stand-ins** when it is not — same
  surface, no exceptions. The stdlib fallback lets :func:`span`
  work as a context manager / decorator with zero behavioural
  change when telemetry is off.

* :func:`span` doubles as a *decorator* — wrap a function,
  with-``as`` a block.

* :func:`record_event` emits a span *event* on the current span
  (without wrapping the call in its own span). Used by
  :class:`nl2pbip.budget.TokenBudget.check_and_record` so every
  charge produces a single correlated event rather than a span
  explosion.

* OTLP exporter failures are *swallowed* and logged — the
  orchestrator's pipeline must keep running even when Jaeger is
  down.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from typing import (
    Any,
    Iterator,
    Mapping,
    Optional,
)

_LOG = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# OpenTelemetry availability detection
# ----------------------------------------------------------------------

# Lazy attribute lookups keep ``import nl2pbip.telemetry`` cheap
# on the no-OTEL path (the common case). We only resolve these
# inside :func:`_configure_tracing` and :func:`get_tracer`.

_TRACING_ENABLED: bool = False
_TRACER_PROVIDER: Any = None
_TRACER: Any = None
_CONFIGURED: bool = False


def is_tracing_enabled() -> bool:
    """Return ``True`` when the OpenTelemetry SDK was configured.

    False in two cases:

    * No SDK installed (stdlib fallback path).
    * SDK installed but ``NL2PBIP_OTEL_EXPORTER=none`` (default).

    The remainder of the module is *idempotent* regardless — the
    ``@span`` decorator and :func:`get_current_span` always
    return safe objects even when this returns ``False``.
    """
    return _TRACING_ENABLED


def get_tracer(name: str) -> Any:
    """Return a tracer.

    Returns the real ``opentelemetry.trace.get_tracer`` proxy
    when the SDK is configured, otherwise a single no-op
    stand-in object (singleton) whose
    ``start_as_current_span(...)`` and ``start_span(...)``
    return an inert context manager.
    """
    if _TRACER is not None:
        return _TRACER
    if _TRACER_PROVIDER is not None:
        try:
            from opentelemetry import trace as _otel_trace

            return _otel_trace.get_tracer(name)
        except Exception:  # pragma: no cover - SDK import race
            return _NOOP_TRACER
    return _NOOP_TRACER


def get_current_span() -> Any:
    """Return the active span (real or no-op).

    Safe to call anywhere — including outside a span. Returns a
    no-op span whose ``set_attribute`` / ``add_event`` are inert.
    """
    if _TRACER_PROVIDER is not None:
        try:
            from opentelemetry import trace as _otel_trace

            return _otel_trace.get_current_span()
        except Exception:  # pragma: no cover - SDK import race
            return _NoopSpan()
    return _NoopSpan()


# ----------------------------------------------------------------------
# Span helper (decorator + context manager)
# ----------------------------------------------------------------------


class _NoopSpan:
    """Inert stand-in for ``opentelemetry.trace.Span``.

    All mutators are no-ops and ``__enter__`` returns ``self``
    so ``with _NoopSpan(): ...`` works in fallback mode.
    """

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def add_event(self, name: str, attrs: Optional[Mapping[str, Any]] = None) -> None:
        return None

    def end(self) -> None:
        return None


class _NoopTracer:
    """Inert stand-in for ``opentelemetry.trace.Tracer``."""

    @contextmanager
    def start_as_current_span(
        self, name: str, *args: Any, **kwargs: Any
    ) -> Iterator[_NoopSpan]:
        yield _NoopSpan()

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


# Module-level singleton used by ``get_tracer`` whenever the
# OpenTelemetry SDK isn't installed (the common no-op path).
_NOOP_TRACER = _NoopTracer()


@contextmanager
def _span_cm(name: str, attrs: Optional[Mapping[str, Any]] = None) -> Iterator[Any]:
    """Implementation behind :func:`span` when used as a ctx mgr."""
    tracer = get_tracer(__name__)
    if not _TRACING_ENABLED:
        # No-op fast-path: still resolve attrs (dict construction
        # is cheap) but never start a real span. We treat the
        # OTEL-disabled case as pure overhead-free.
        yield _NoopSpan().__enter__()
        return
    # Real path: SDK is installed *and* configured.
    span_cm = tracer.start_as_current_span(name)
    with span_cm as span:
        if attrs:
            for key, value in attrs.items():
                try:
                    span.set_attribute(key, value)
                except Exception:  # pragma: no cover - defensive
                    pass  # nosec B110 — best-effort span attribute setting
        try:
            yield span
        except Exception as exc:
            try:
                span.record_exception(exc)
            except Exception:  # pragma: no cover - defensive
                pass  # nosec B110 — best-effort exception recording
            raise


def span(
    name: str,
    attrs: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Dual-purpose span helper.

    Used as a **context manager**::

        with span("nl2pbip.run", {"prompt_version": 7}):
            ...

    Used as a **decorator**::

        @span("nl2pbip.llm.chat")
        def chat(self, messages):
            ...

    When OTEL is disabled, both forms are zero-overhead no-ops —
    :func:`is_tracing_enabled` gates every real start.
    """

    def decorate(fn):  # type: ignore[no-untyped-def]
        # Build attrs lazily so attribute values captured at
        # definition time don't pin state across invocations.
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            with _span_cm(name, attrs):
                return fn(*args, **kwargs)

        return wrapper

    # ``@span("name")`` (no second arg) → returns the decorator.
    # ``with span("name") as s:`` → returns the context manager.
    return _span_cm(name, attrs)


def record_event(name: str, attrs: Optional[Mapping[str, Any]] = None) -> None:
    """Attach an *event* (not a span) to the current span.

    Used by ``TokenBudget.check_and_record`` to log per-call
    spend without wrapping the budget call in its own span
    (events are cheaper and surface fine-grained time series
    inside the parent span).
    """
    if not _TRACING_ENABLED:
        return
    span_obj = get_current_span()
    try:
        span_obj.add_event(name, dict(attrs) if attrs else None)
    except Exception:  # pragma: no cover - defensive against broken SDK
        return


# ----------------------------------------------------------------------
# Public singleton + configuration
# ----------------------------------------------------------------------

TRACER_PROVIDER: Any = None
"""Module-level singleton set by :func:`_configure_tracing`."""


def _configure_tracing() -> Any:
    """Initialise the tracer provider from environment variables.

    Called lazily on first import. Idempotent — repeated calls
    return the existing ``TRACER_PROVIDER`` without re-creating
    it (so test fixtures can call this freely).

    Returns the tracer provider (real SDK object or ``None``).
    """
    global TRACER_PROVIDER, _TRACING_ENABLED, _TRACER, _CONFIGURED

    if _CONFIGURED:
        return TRACER_PROVIDER
    _CONFIGURED = True

    exporter_name = os.environ.get("NL2PBIP_OTEL_EXPORTER", "none").strip().lower()
    if exporter_name == "none":
        _TRACING_ENABLED = False
        return None

    # Try to import the SDK lazily. If the SDK isn't installed
    # we silently fall back to no-op — telemetry is opt-in.
    try:
        from opentelemetry import trace as _otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
    except Exception as exc:  # pragma: no cover - SDK not installed
        _LOG.debug("OpenTelemetry SDK not installed; telemetry disabled: %s", exc)
        _TRACING_ENABLED = False
        return None

    service_name = os.environ.get("NL2PBIP_OTEL_SERVICE_NAME", "nl2pbip")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if exporter_name == "console":
        out_target = (
            os.environ.get("NL2PBIP_OTEL_CONSOLE_OUT", "stderr").strip().lower()
        )
        if out_target == "stdout":
            stream = sys.stdout
        else:
            stream = sys.stderr
        try:
            exporter = ConsoleSpanExporter(out=stream)
        except TypeError:
            # Older SDKs accept ``out=``; newer accept only no-arg.
            exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
    elif exporter_name == "otlp_http":
        endpoint = os.environ.get("NL2PBIP_OTEL_OTLP_ENDPOINT", "http://localhost:4318")
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except Exception as exc:  # pragma: no cover - optional dep
            _LOG.warning(
                "OTLP exporter requested but opentelemetry-exporter-otlp-proto-http "
                "is not installed (%s); falling back to no exporter.",
                exc,
            )
            return None
        try:
            exporter = OTLPSpanExporter(endpoint=endpoint + "/v1/traces")
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning(
                "Failed to initialise OTLP exporter at %s (%s); telemetry disabled.",
                endpoint,
                exc,
            )
            return None
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        # Unknown exporter → no exporter, but provider still set
        # so spans are recorded in-process. The user can later
        # hook in a real one without touching nl2pbip.
        _LOG.warning(
            "Unknown NL2PBIP_OTEL_EXPORTER=%r; provider created with no exporter.",
            exporter_name,
        )

    _otel_trace.set_tracer_provider(provider)
    TRACER_PROVIDER = provider
    _TRACER_PROVIDER = provider
    _TRACING_ENABLED = True
    return TRACER_PROVIDER


def reset_for_testing() -> None:
    """Reset module-level state — *test-only* helper.

    Forces the next :func:`_configure_tracing` call to re-read
    environment variables. Production code never calls this;
    it's exposed for tests that need to swap env vars between
    cases without leaking state across them.
    """
    global TRACER_PROVIDER, _TRACING_ENABLED, _TRACER, _CONFIGURED, _TRACER_PROVIDER
    TRACER_PROVIDER = None
    _TRACING_ENABLED = False
    _TRACER = None
    _TRACER_PROVIDER = None
    _CONFIGURED = False


# ----------------------------------------------------------------------
# Lazy initialisation at first import.
# ----------------------------------------------------------------------

_configure_tracing()


__all__ = [
    "TRACER_PROVIDER",
    "get_current_span",
    "get_tracer",
    "is_tracing_enabled",
    "record_event",
    "reset_for_testing",
    "span",
]
