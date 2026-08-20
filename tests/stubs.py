"""Provider stubs shared by the router tests.

Both implement `LLMProvider` directly rather than subclassing `MockProvider`, so that mypy checks
them against the protocol the production code depends on. Subclassing the mock and narrowing one
signature would type-check as a subclass problem instead of as what it is: a second implementation
of the seam.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from consilium.llm.base import Delta, LLMResponse, Message, ToolSchema, Usage
from consilium.trace import Tracer


class FailingProvider:
    """Raises on every call. Used to prove a provider outage does not end a turn."""

    name = "failing"
    model = "none"

    def __init__(self, error: str = "upstream is down") -> None:
        self.error = error
        self.calls = 0

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, tools, tracer, caller, kwargs
        self.calls += 1
        raise RuntimeError(self.error)

    def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        del messages, tracer, caller, kwargs
        raise NotImplementedError("FailingProvider does not stream")


class RecordingProvider:
    """Answers with fixed content and records what it was offered."""

    name = "recording"
    model = "recording-model"

    def __init__(self, content: str = "") -> None:
        self.content = content
        self.tools_offered: list[list[str] | None] = []
        self.callers: list[str | None] = []
        self.messages: list[list[Message]] = []

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del tracer, kwargs
        self.messages.append(list(messages))
        self.callers.append(caller)
        self.tools_offered.append(
            None if tools is None else [str(tool["function"]["name"]) for tool in tools]
        )
        return LLMResponse(
            content=self.content,
            provider=self.name,
            model=self.model,
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        del messages, tracer, caller, kwargs
        raise NotImplementedError("RecordingProvider does not stream")
