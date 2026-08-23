"""Provider stubs shared by the router, API and CLI tests.

Each implements `LLMProvider` directly rather than subclassing `MockProvider`, so that mypy checks
them against the protocol the production code depends on. Subclassing the mock and narrowing one
signature would type-check as a subclass problem instead of as what it is: a second implementation
of the seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
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


class TurnProvider:
    """Answers a whole turn: a plan for the planner, then text for whoever the plan named.

    Branching on `caller` rather than on the message content is what makes it usable under
    concurrency. A script keyed by the last user message cannot tell the planner's call from the
    specialist's -- both carry the same question -- so two turns racing would consume each other's
    replies and the test would be measuring the fixture.

    Every call is recorded with the messages it was given, which is how the API concurrency test
    asserts the property it cares about: no single call ever saw two sessions' questions.

    It yields control before answering, so two turns driven by `asyncio.gather` actually interleave
    rather than running to completion one after the other. A coroutine with no suspension point
    would let the event loop finish the first turn before starting the second, and a concurrency
    test that never achieved concurrency would pass against a shared buffer.
    """

    name = "turn"
    model = "turn-model"

    def __init__(
        self,
        *,
        agents: Sequence[str] = ("consultation",),
        answer: str | Callable[[str], str] = "Blood pressure is measured in mmHg.",
        merged: str = "Merged perspectives.",
        pause: float = 0.0,
    ) -> None:
        self.agents = tuple(agents)
        self.answer = answer
        self.merged = merged
        self.pause = pause
        self.calls: list[tuple[str | None, list[Message]]] = []
        #: The most calls that were ever in flight at once. Evidence that a test that meant to run
        #: two turns concurrently actually did.
        self.max_in_flight = 0
        self._in_flight = 0

    def _plan(self) -> str:
        subtasks = ", ".join(
            f'{{"agent": "{agent}", "objective": "Answer it.", "why": "test"}}'
            for agent in self.agents
        )
        return f'{{"subtasks": [{subtasks}]}}'

    def _content(self, caller: str | None, messages: Sequence[Message]) -> str:
        if caller == "planner":
            return self._plan()
        if caller == "synthesizer":
            return self.merged
        if callable(self.answer):
            last = next((m.content or "" for m in reversed(messages) if m.role == "user"), "")
            return self.answer(last)
        return self.answer

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del tools, tracer, kwargs
        self.calls.append((caller, list(messages)))
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(self.pause)
            return LLMResponse(
                content=self._content(caller, messages),
                provider=self.name,
                model=self.model,
                usage=Usage(prompt_tokens=1, completion_tokens=1),
            )
        finally:
            self._in_flight -= 1

    def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        del messages, tracer, caller, kwargs
        raise NotImplementedError("TurnProvider does not stream")
