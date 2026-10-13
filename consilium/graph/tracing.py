"""Graph layer: LangSmith tracing, on when configured and silent when not.

LangChain and LangGraph read ``LANGSMITH_TRACING``, ``LANGSMITH_API_KEY`` and ``LANGSMITH_PROJECT``
themselves; nothing here passes a key anywhere.  What this module adds is the fallback the brief
asks for: ``LANGSMITH_TRACING=true`` with no key would make every run log an upload error, so in
that case tracing is switched off before the first run and the fact is logged once.  The project
trace (``runs/<session>/<turn>.jsonl``) is written regardless; LangSmith is a second view of the
same turn, never the source of a reported number.
"""

from __future__ import annotations

import os

from consilium.log import get_logger

log = get_logger(__name__)

DEFAULT_PROJECT = "consilium-health"
_TRUE = {"1", "true", "yes", "on"}


def configure_langsmith() -> bool:
    """Return whether LangSmith tracing is on for this process, disabling it if it cannot work."""
    requested = os.environ.get("LANGSMITH_TRACING", os.environ.get("LANGCHAIN_TRACING_V2", ""))
    if requested.strip().lower() not in _TRUE:
        return False
    key = os.environ.get("LANGSMITH_API_KEY", os.environ.get("LANGCHAIN_API_KEY", "")).strip()
    if not key:
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ.pop("LANGCHAIN_TRACING_V2", None)
        log.warning("graph.langsmith_disabled", reason="LANGSMITH_TRACING is set but no API key is")
        return False
    os.environ.setdefault("LANGSMITH_PROJECT", DEFAULT_PROJECT)
    log.info("graph.langsmith_enabled", project=os.environ["LANGSMITH_PROJECT"])
    return True
