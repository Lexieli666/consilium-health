"""Substrate: the Anthropic provider, behind the same protocol.

A second real provider is not redundancy for its own sake.  It is what makes ``LLMProvider`` a
seam rather than a wrapper: the two APIs differ in ways that a single-provider abstraction would
have quietly encoded as universal, and each difference is handled in one place here.

Three of them are worth naming, because they are exactly what a one-provider design would get wrong:

* **The system prompt is a top-level parameter**, not a message with ``role: "system"``.  Neutral
  ``system`` messages are lifted out and concatenated.
* **Tool results are user-turn content blocks**, not a ``tool`` role.  Consecutive neutral ``tool``
  messages are merged into one user message of ``tool_result`` blocks.
* **Tool schemas are ``{name, description, input_schema}``**, not the OpenAI function envelope, so
  the registry's OpenAI-format schemas are unwrapped rather than regenerated.  One derivation, two
  wire formats.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

DEFAULT_MODEL = "claude-sonnet-4-5"

#: Anthropic requires ``max_tokens``; there is no "as much as you need" value.  Sized for a grounded
#: paragraph-length answer plus an escalation banner, not for an essay.
DEFAULT_MAX_TOKENS = 1500

RETRYABLE: tuple[type[Exception], ...] = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)

MAX_ATTEMPTS = 4

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def to_anthropic_tools(tools: Sequence[ToolSchema]) -> list[dict[str, Any]]:
    """Unwrap OpenAI-format tool definitions into Anthropic's shape.

    Unwrapped rather than regenerated from the Pydantic models a second time: one derivation with
    two presentations cannot drift, two derivations can.
    """
    unwrapped: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function", tool)
        unwrapped.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return unwrapped


def to_anthropic_messages(messages: Sequence[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """Split neutral messages into the system prompt and the message list."""
    system_parts: list[str] = []
    wire: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue

        if message.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.content or "",
            }
            # Consecutive tool results belong to one user turn.  Appending them as separate
            # messages is accepted by the API but produces a transcript the model reads as several
            # turns of user input, which is not what happened.
            if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                wire[-1]["content"].append(block)
            else:
                wire.append({"role": "user", "content": [block]})
            continue

        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            wire.append({"role": "assistant", "content": blocks})
            continue

        wire.append({"role": message.role, "content": message.content or ""})

    return ("\n\n".join(system_parts) if system_parts else None), wire


def from_anthropic_message(payload: Any, *, model: str, latency_ms: float) -> LLMResponse:
    """Translate an Anthropic message into the neutral response type."""
    text: list[str] = []
    calls: list[ToolCall] = []
    for block in payload.content:
        if block.type == "text":
            text.append(block.text)
        elif block.type == "tool_use":
            arguments = block.input if isinstance(block.input, dict) else {}
            calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

    return LLMResponse(
        content="".join(text) or None,
        tool_calls=calls,
        stop_reason=_STOP_REASONS.get(payload.stop_reason or "", "other"),
        provider="anthropic",
        model=payload.model or model,
        usage=Usage(
            prompt_tokens=getattr(payload.usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(payload.usage, "output_tokens", 0) or 0,
        ),
        latency_ms=latency_ms,
    )


class AnthropicProvider:
    """Messages API and streaming, behind :class:`~consilium.llm.base.LLMProvider`."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_multiplier: float = 1.0,
    ) -> None:
        self.name = "anthropic"
        self.model = model
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        # The backoff floor.  Exposed because a caller running a 600-item evaluation sweep against a
        # rate-limited endpoint wants a different curve from an interactive CLI, and because a test
        # of the retry behaviour should not spend a second per attempt proving tenacity waits.
        self.backoff_multiplier = backoff_multiplier
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=self.backoff_multiplier, min=0, max=20),
            reraise=True,
        )

    def _request(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        system, wire = to_anthropic_messages(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": wire,
            **kwargs,
        }
        if system is not None:
            request["system"] = system
        if tools:
            request["tools"] = to_anthropic_tools(tools)
        return request

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        tracer: Tracer | None = None,
        caller: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        request = self._request(messages, tools, kwargs)

        with stopwatch() as elapsed_ms:
            async for attempt in self._retrying():
                with attempt:
                    payload = await self._client.messages.create(**request)

        response = from_anthropic_message(payload, model=self.model, latency_ms=elapsed_ms())
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
        request = self._request(messages, None, kwargs)

        content: list[str] = []
        usage = Usage()
        stop_reason: StopReason = "stop"

        with stopwatch() as elapsed_ms:
            async with self._client.messages.stream(**request) as stream:
                async for piece in stream.text_stream:
                    content.append(piece)
                    yield Delta(content=piece)
                final = await stream.get_final_message()
                stop_reason = _STOP_REASONS.get(final.stop_reason or "", "other")
                usage = Usage(
                    prompt_tokens=final.usage.input_tokens or 0,
                    completion_tokens=final.usage.output_tokens or 0,
                )

        yield Delta(stop_reason=stop_reason, usage=usage)
        record_llm_call(
            tracer,
            caller,
            LLMResponse(
                content="".join(content),
                stop_reason=stop_reason,
                provider=self.name,
                model=self.model,
                usage=usage,
                latency_ms=elapsed_ms(),
            ),
            [],
        )
