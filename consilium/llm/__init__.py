"""Substrate: the LLM provider seam.

Everything above this package talks to a model through :class:`~consilium.llm.base.LLMProvider` and
never imports a vendor SDK, which is what makes the same agent code runnable against a real
provider, a second real provider, or a scripted mock.
"""

from consilium.llm.anthropic_provider import AnthropicProvider
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
from consilium.llm.factory import ProviderError, make_provider
from consilium.llm.mock import MockProvider, ScriptedResponse, ScriptExhaustedError
from consilium.llm.openai_provider import OpenAIProvider

__all__ = [
    "AnthropicProvider",
    "Delta",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "MockProvider",
    "OpenAIProvider",
    "ProviderError",
    "ScriptExhaustedError",
    "ScriptedResponse",
    "StopReason",
    "ToolCall",
    "ToolSchema",
    "Usage",
    "make_provider",
    "record_llm_call",
]
