"""Substrate: a scripted provider, so the whole system is exercisable with no key and no network.

This is one half of the offline rule.  The suite does not monkeypatch ``openai`` or intercept HTTP;
it injects a different implementation of the same protocol, which is the seam a reviewer can point
at.  ``MockProvider`` implements ``stream_chat`` as well as ``chat`` so the SSE endpoint is testable
without a provider.

Token counts are synthetic unless a script supplies them.  No number reported anywhere in the
project may come from a ``MockProvider`` run; the evaluation harness requires a live provider.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from consilium.llm.base import (
    Delta,
    LLMResponse,
    Message,
    StopReason,
    ToolCall,
    ToolSchema,
    Usage,
    record_llm_call,
    tool_names,
)
from consilium.trace import Tracer, stopwatch

#: Rough characters-per-token used only when a script does not state usage.  Deliberately crude:
#: a precise-looking fake would invite someone to treat mock token counts as measurements.
_CHARS_PER_TOKEN = 4


class ScriptExhaustedError(RuntimeError):
    """Raised when the script has no response left for a call.

    Failing loudly is the point.  A mock that invents a reply when the script runs out turns a test
    that was meant to pin down behaviour into a test that passes for the wrong reason.
    """


class ScriptedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ScriptedResponse(BaseModel):
    """One scripted reply.

    ``when`` is an optional case-insensitive substring of the most recent user message.  Selection
    prefers an unconsumed response whose ``when`` matches; failing that it takes the next unconsumed
    response with no ``when``.  That keeps ordinary sequential scripts trivial while letting a test
    pin one reply to one question without counting call positions.
    """

    model_config = ConfigDict(extra="forbid")

    when: str | None = None
    content: str | None = None
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)
    stop_reason: StopReason | None = None
    usage: Usage | None = None


class _Script(BaseModel):
    model_config = ConfigDict(extra="forbid")

    responses: list[ScriptedResponse]


class MockProvider:
    """Replays scripted responses.  Deterministic, offline, and loud when misused."""

    def __init__(
        self,
        responses: Sequence[ScriptedResponse],
        *,
        model: str = "mock-model",
        chunk_size: int = 16,
    ) -> None:
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1; got {chunk_size}")
        self.name = "mock"
        self.model = model
        self.chunk_size = chunk_size
        self._responses = list(responses)
        self._consumed: set[int] = set()
        self._ids = itertools.count(1)

    @classmethod
    def from_file(cls, path: Path, **kwargs: Any) -> MockProvider:
        """Load a YAML fixture of the form ``{responses: [...]}``."""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        script = _Script.model_validate(raw)
        return cls(script.responses, **kwargs)

    @property
    def remaining(self) -> int:
        """How many scripted responses have not been consumed."""
        return len(self._responses) - len(self._consumed)

    def reset(self) -> None:
        """Make every scripted response available again."""
        self._consumed.clear()

    def _select(self, messages: Sequence[Message]) -> ScriptedResponse:
        last_user = next(
            (m.content or "" for m in reversed(messages) if m.role == "user"),
            "",
        ).lower()

        for index, response in enumerate(self._responses):
            if index in self._consumed or response.when is None:
                continue
            if response.when.lower() in last_user:
                self._consumed.add(index)
                return response

        for index, response in enumerate(self._responses):
            if index in self._consumed or response.when is not None:
                continue
            self._consumed.add(index)
            return response

        raise ScriptExhaustedError(
            f"MockProvider script exhausted after {len(self._consumed)} response(s); "
            f"last user message was {last_user[:120]!r}"
        )

    def _usage(self, messages: Sequence[Message], response: ScriptedResponse) -> Usage:
        if response.usage is not None:
            return response.usage
        prompt_chars = sum(len(m.content or "") for m in messages)
        completion_chars = len(response.content or "")
        return Usage(
            prompt_tokens=max(1, prompt_chars // _CHARS_PER_TOKEN),
            completion_tokens=max(1, completion_chars // _CHARS_PER_TOKEN),
        )

    def _build(
        self, messages: Sequence[Message], response: ScriptedResponse, latency_ms: float
    ) -> LLMResponse:
        calls = [
            ToolCall(id=f"call_{next(self._ids)}", name=call.name, arguments=call.arguments)
            for call in response.tool_calls
        ]
        stop_reason: StopReason = response.stop_reason or ("tool_calls" if calls else "stop")
        return LLMResponse(
            content=response.content,
            tool_calls=calls,
            stop_reason=stop_reason,
            provider=self.name,
            model=self.model,
            usage=self._usage(messages, response),
            latency_ms=latency_ms,
        )

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del kwargs  # accepted for protocol compatibility; a mock has no sampling parameters
        with stopwatch() as elapsed:
            scripted = self._select(messages)
        response = self._build(messages, scripted, elapsed())
        record_llm_call(tracer, caller, response, tool_names(tools))
        return response

    async def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        """Yield the scripted answer in fixed-size chunks, then a final delta with usage.

        Chunking by characters rather than by token is deliberate: the point is to prove the SSE
        path assembles deltas correctly, and pretending to emit real tokenizer boundaries would be
        a fake measurement dressed as a test fixture.
        """
        del kwargs
        with stopwatch() as elapsed:
            scripted = self._select(messages)
        response = self._build(messages, scripted, elapsed())

        text = response.content or ""
        for start in range(0, len(text), self.chunk_size):
            yield Delta(content=text[start : start + self.chunk_size])

        yield Delta(stop_reason=response.stop_reason, usage=response.usage)
        record_llm_call(tracer, caller, response, [])
