"""Substrate: structured logging.

Logs and traces are different artifacts and are not interchangeable.  ``trace.py`` writes the
append-only, schema-validated record that every evaluation number is computed from; this module
writes operational logs for a human reading a terminal or a log aggregator.  Nothing in
docs/EVALUATION.md may be derived from these records.

Named ``log`` rather than ``logging`` so that no reader has to reason about whether an
``import logging`` inside the package resolves to the standard library.
"""

from __future__ import annotations

import logging
from typing import Any, TextIO

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.typing import EventDict, WrappedLogger

_TURN_FIELDS = ("session_id", "trace_id", "turn_index")


def ensure_turn_fields(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Guarantee that every record carries the turn identifiers, even outside a turn.

    The engineering baseline promises ``session_id`` and ``trace_id`` on every record.  Binding
    them in context vars achieves that inside a turn; this processor makes the promise literally
    true by writing an explicit ``None`` when there is no turn in scope, so a downstream query on
    the field never silently drops records.
    """
    for field in _TURN_FIELDS:
        event_dict.setdefault(field, None)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json", stream: TextIO | None = None) -> None:
    """Configure structlog process-wide.

    ``cache_logger_on_first_use`` is off so that a test or a CLI flag can reconfigure logging after
    a logger has already been created; at this scale the per-call cost is irrelevant next to an
    LLM round trip.

    ``stream`` defaults to ``None``, which is structlog's own default of ``sys.stdout``.  It exists
    for one caller: the MCP server's stdio transport, where stdout carries JSON-RPC frames and a
    log line written into it is a protocol error rather than a cosmetic problem.  A flag on the
    logging setup rather than a redirect at the entry point, because the entry point would have to
    redirect *around* every library that might log, and this is the one place that decision is made.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            ensure_turn_fields,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.WriteLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_turn(*, session_id: str, trace_id: str, turn_index: int) -> None:
    """Bind the turn identifiers for the current context (task-local, not process-global)."""
    bind_contextvars(session_id=session_id, trace_id=trace_id, turn_index=turn_index)


def clear_turn() -> None:
    """Clear turn identifiers bound by :func:`bind_turn`."""
    clear_contextvars()
