"""Retain parser reconciliation warnings under explicit annotated policy."""

from contextvars import ContextVar
from dataclasses import replace
from typing import Callable

from .models import Snapshot


_NOTES: ContextVar[list[str] | None] = ContextVar("source_notes", default=None)


def annotated_active() -> bool:
    """Report whether the caller explicitly selected soft source quality."""
    return _NOTES.get() is not None


def discrepancy(message: str) -> None:
    """Warn inside annotated parsing; keep direct strict diagnostics intact."""
    notes = _NOTES.get()
    if notes is None:
        raise ValueError(message)
    notes.append(message)


def parse_annotated(parser: Callable, raw: bytes, etf,
                    fetched_at: str) -> Snapshot:
    """Capture only explicitly softened quality checks, never all errors.

    Source identity, date, currency, unit, finite numeric and HTTPS failures
    still propagate. A context-local ledger avoids cross-request leakage.
    """
    notes: list[str] = []
    token = _NOTES.set(notes)
    try:
        snapshot = parser(raw, etf, fetched_at)
        return replace(snapshot, quality_notes=tuple(dict.fromkeys(
            snapshot.quality_notes + tuple(notes))))
    finally:
        _NOTES.reset(token)
