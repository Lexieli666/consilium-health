"""Substrate: provider-agnostic message, response and streaming types.

``stream_chat`` is part of the protocol rather than an extra that one implementation happens to
offer, because ``POST /v1/chat`` streams.  A streaming endpoint layered over a non-streaming
provider interface is a refactor waiting to happen: it forces the endpoint to either buffer the
whole answer (and lose the point of streaming) or to reach around the abstraction.

Scope decision, stated once here and again in docs/DESIGN.md: streaming carries *answer* tokens
only, not tool-call deltas.  Tool calls happen inside the ReAct loop, which is not streamed to the
client; only the final answer is.  Streamed tool calls would add per-provider partial-JSON
accumulation for no user-visible benefit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from consilium.trace import Tracer

Role = Literal["system", "user", "assistant", "tool"]

StopReason = Literal["stop", "tool_calls", "length", "content_filter", "error", "other"]

#: An OpenAI-format tool definition.  Produced by ``SkillRegistry.to_tool_schemas()`` from the
#: skills' Pydantic argument models; kept as a plain mapping here so the LLM layer does not depend
#: on the Skills layer above it.
ToolSchema: TypeAlias = dict[str, Any]


class ToolCall(BaseModel):
    """A model's request to run one skill."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One chat message in provider-neutral form."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class Usage(BaseModel):
    """Token accounting for one call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMResponse(BaseModel):
    """A completed non-streaming call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: StopReason = "stop"
    provider: str
    model: str
    usage: Usage = Usage()
    latency_ms: float = Field(default=0.0, ge=0)


class Delta(BaseModel):
    """One increment of a streamed answer.

    The last delta of a stream carries ``stop_reason`` and ``usage``; content deltas carry neither.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str | None = None
    stop_reason: StopReason | None = None
    usage: Usage | None = None


class LLMProvider(Protocol):
    """The only interface any layer above the substrate uses to reach a model."""

    name: str
    model: str

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse: ...

    def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]: ...


def record_llm_call(
    tracer: Tracer | None,
    caller: str | None,
    response: LLMResponse,
    tools_offered: Sequence[str],
) -> None:
    """Emit the ``llm_call`` trace event for a completed call.

    Providers call this themselves rather than leaving it to the caller.  Cost per turn is summed
    over *all* ``llm_call`` events including the planner's and the synthesizer's, and a call site
    that forgets to log is a call site that silently makes the architecture look cheaper than it is.
    """
    if tracer is None or caller is None:
        return
    tracer.llm_call(
        caller=caller,
        provider=response.provider,
        model=response.model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        latency_ms=response.latency_ms,
        tools_offered=list(tools_offered),
        stop_reason=response.stop_reason,
    )


def tool_names(tools: Sequence[ToolSchema] | None) -> list[str]:
    """Extract tool names from OpenAI-format tool definitions for the trace event."""
    if not tools:
        return []
    names: list[str] = []
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str):
                names.append(name)
    return names
