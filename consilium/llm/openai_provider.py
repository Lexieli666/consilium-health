"""Substrate: the OpenAI-compatible provider.

The module is ``openai_provider.py`` rather than ``openai.py``.  The shorter name would work --
Python 3 has no implicit relative imports, so ``import openai`` inside it still finds the vendor
package -- but a file that shadows the library it imports is a trap for the next reader, and the
cost of avoiding it is eight characters.

Two translation functions do the real work and are pure, so they are tested without a network call:
:func:`to_openai_messages` maps the provider-neutral :class:`Message` onto the wire format, and
:func:`from_openai_choice` maps a completion back.  Only :meth:`OpenAIProvider.chat` and
:meth:`OpenAIProvider.stream_chat` touch the client, and those are the only parts marked
``network``.

Retries are ``tenacity`` with exponential backoff on rate limits and 5xx, and **not** on a 400.
A malformed request retried three times is three times the latency and the same error; the class of
failure that retrying fixes is transient by definition.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
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

DEFAULT_MODEL = "gpt-4o-mini"

#: Retried: the transient ones.  ``BadRequestError`` and ``AuthenticationError`` are deliberately
#: absent -- neither becomes true on the second attempt.
RETRYABLE: tuple[type[Exception], ...] = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)

MAX_ATTEMPTS = 4

#: OpenAI's ``finish_reason`` values mapped onto the neutral :data:`StopReason` literal.  An
#: unrecognized value becomes ``other`` rather than raising: a new finish reason is a provider
#: change, and dropping a completed answer because of a label would be the wrong trade.
_STOP_REASONS: dict[str, StopReason] = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "length",
    "content_filter": "content_filter",
}


def to_openai_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Translate neutral messages onto the chat-completions wire format."""
    wire: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {"role": message.role}
        if message.content is not None:
            entry["content"] = message.content
        if message.name is not None:
            entry["name"] = message.name
        if message.tool_call_id is not None:
            entry["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ]
            # The API requires the key to be present on an assistant message that carries tool
            # calls, even when the model produced no prose alongside them.
            entry.setdefault("content", None)
        wire.append(entry)
    return wire


def parse_tool_arguments(raw: str | None, *, call_id: str) -> dict[str, Any]:
    """Parse a tool call's JSON arguments, degrading to an empty mapping on malformed JSON.

    The model, not the code, produced this string, so it can be invalid JSON.  Raising here would
    end the turn; returning ``{}`` lets the skill's own validator reject it and hand the model a
    message it can act on within its tool budget -- which is where every other bad-argument case is
    already handled.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"__unparsed_arguments__": raw, "__tool_call_id__": call_id}
    return parsed if isinstance(parsed, dict) else {"__unparsed_arguments__": raw}


def from_openai_choice(payload: Any, *, model: str, latency_ms: float) -> LLMResponse:
    """Translate a chat completion into the neutral response type."""
    choice = payload.choices[0]
    message = choice.message
    calls = [
        ToolCall(
            id=call.id,
            name=call.function.name,
            arguments=parse_tool_arguments(call.function.arguments, call_id=call.id),
        )
        for call in (message.tool_calls or [])
    ]
    usage = payload.usage
    return LLMResponse(
        content=message.content,
        tool_calls=calls,
        stop_reason=_STOP_REASONS.get(choice.finish_reason or "", "other"),
        provider="openai",
        model=payload.model or model,
        usage=Usage(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        ),
        latency_ms=latency_ms,
    )


class OpenAIProvider:
    """Chat completions and streaming against an OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        client: Any | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_multiplier: float = 1.0,
    ) -> None:
        self.name = "openai"
        self.model = model
        self.max_attempts = max_attempts
        # The backoff floor.  Exposed because a caller running a 600-item evaluation sweep against a
        # rate-limited endpoint wants a different curve from an interactive CLI, and because a test
        # of the retry behaviour should not spend a second per attempt proving tenacity waits.
        self.backoff_multiplier = backoff_multiplier
        self._client = client or openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=self.backoff_multiplier, min=0, max=20),
            reraise=True,
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
        request: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            **kwargs,
        }
        if tools:
            request["tools"] = list(tools)

        with stopwatch() as elapsed_ms:
            async for attempt in self._retrying():
                with attempt:
                    payload = await self._client.chat.completions.create(**request)

        response = from_openai_choice(payload, model=self.model, latency_ms=elapsed_ms())
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
        """Stream answer tokens.  Tool calls are not streamed; see ``consilium/llm/base.py``."""
        request: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }

        content: list[str] = []
        usage = Usage()
        stop_reason: StopReason = "stop"

        with stopwatch() as elapsed_ms:
            stream = await self._client.chat.completions.create(**request)
            async for event in stream:
                if event.usage is not None:
                    usage = Usage(
                        prompt_tokens=event.usage.prompt_tokens or 0,
                        completion_tokens=event.usage.completion_tokens or 0,
                    )
                for choice in event.choices:
                    if choice.finish_reason:
                        stop_reason = _STOP_REASONS.get(choice.finish_reason, "other")
                    piece = choice.delta.content
                    if piece:
                        content.append(piece)
                        yield Delta(content=piece)

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
