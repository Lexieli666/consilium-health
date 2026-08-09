"""The engineering baseline promises ``session_id`` and ``trace_id`` on every log record.  That is a
property of the processor chain, so the processor is tested directly rather than through a captured
logger, which would bypass it.
"""

from __future__ import annotations

import structlog

from consilium.log import bind_turn, clear_turn, configure_logging, ensure_turn_fields, get_logger


def test_turn_fields_are_present_even_outside_a_turn() -> None:
    event_dict = ensure_turn_fields(None, "info", {"event": "startup"})

    assert event_dict["session_id"] is None
    assert event_dict["trace_id"] is None
    assert event_dict["turn_index"] is None


def test_bound_turn_fields_are_not_overwritten() -> None:
    event_dict = ensure_turn_fields(
        None, "info", {"event": "answered", "session_id": "s1", "trace_id": "t1", "turn_index": 2}
    )

    assert event_dict["session_id"] == "s1"
    assert event_dict["trace_id"] == "t1"
    assert event_dict["turn_index"] == 2


def test_bind_turn_populates_context_vars() -> None:
    try:
        bind_turn(session_id="s1", trace_id="t1", turn_index=3)
        context = structlog.contextvars.get_contextvars()

        assert context["session_id"] == "s1"
        assert context["trace_id"] == "t1"
        assert context["turn_index"] == 3
    finally:
        clear_turn()

    assert structlog.contextvars.get_contextvars() == {}


def test_configure_logging_accepts_both_renderers() -> None:
    for fmt in ("json", "console"):
        configure_logging(level="DEBUG", fmt=fmt)
        assert get_logger("test") is not None
