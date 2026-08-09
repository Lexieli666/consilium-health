"""Substrate: the LLM provider seam.

Everything above this package talks to a model through :class:`~consilium.llm.base.LLMProvider` and
never imports a vendor SDK, which is what makes the same agent code runnable against a real
provider, a second real provider, or a scripted mock.
"""

from consilium.llm.base import (
    Delta,
    LLMProvider,
    LLMResponse,
    Message,
    StopReason,
    ToolCall,
    ToolSchema,
    Usage,
    record_llm_call,
)
from consilium.llm.mock import MockProvider, ScriptedResponse, ScriptExhaustedError

__all__ = [
    "Delta",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "MockProvider",
    "ScriptExhaustedError",
    "ScriptedResponse",
    "StopReason",
    "ToolCall",
    "ToolSchema",
    "Usage",
    "record_llm_call",
]
