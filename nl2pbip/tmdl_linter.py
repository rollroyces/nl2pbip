"""Lightweight TMDL validator used by the orchestrator's retry loop.

When a TMDL-emitting handler raises ``TMDLValidationError``, the orchestrator
catches it (``orchestrator.run``), formats a feedback message, and re-invokes
the LLM planner with the error attached. This module exists to give the
engine a single import path for that exception type.
"""

from __future__ import annotations


class TMDLValidationError(Exception):
    """Raised when a TMDL-emitting tool call produces an invalid model."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return super().__str__()


__all__ = ["TMDLValidationError"]
