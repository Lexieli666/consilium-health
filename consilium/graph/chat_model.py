"""Graph layer: the project's ``LLMProvider`` behind LangChain's chat-model interface.

**The graph calls the same provider objects the legacy router calls**, through a thin
:class:`~langchain_core.language_models.BaseChatModel` that converts messages in and out.  The
alternative -- ``ChatOpenAI`` next to ``consilium.llm.OpenAIProvider`` -- would mean two OpenAI
clients configured from two places, two pricing ids, and ``llm_call`` events emitted by a callback
that re-derives what the provider already records.  Going through the provider keeps every token the
graph spends in the trace by construction: ``LLMProvider.chat`` calls ``record_llm_call`` itself,
and that one event is what cost per turn is summed from on every orchestrator.

It also keeps the offline rule.  ``CONSILIUM_PROVIDER=mock`` runs the whole graph on
:class:`consilium.llm.MockProvider` with no key and no network, which is how the tests and the
``--mock`` demo exercise the planner, the tool node, the synthesizer and the repair loop.

``bind_tools`` converts the LangChain tools to the OpenAI-format tool definitions the provider
protocol takes (:func:`~langchain_core.utils.function_calling.convert_to_openai_tool`); for the
MCP tools in ``mcp_tools.py`` that is the server's own ``inputSchema`` passed through unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

from consilium.llm.base import LLMResponse, Message, ToolCall, ToolSchema


class ProviderChatModel(BaseChatModel):
    """A LangChain chat model whose generations come from a Consilium ``LLMProvider``.

    ``caller`` is passed per invocation (``model.ainvoke(messages, caller="planner")``) and becomes
    ``llm_call.caller`` in the trace; the tracer is looked up per call through ``tracer_for`` so
    the compiled graph can be built once per process and still write each turn to its own trace.
    """

    #: An :class:`~consilium.llm.base.LLMProvider`.  Typed ``Any`` because the provider is a
    #: ``Protocol`` that pydantic (which validates these fields) cannot ``isinstance`` against.
    provider: Any
    #: ``Callable[[], Tracer | None]``: the tracer of the turn in flight, or ``None`` outside one.
    tracer_for: Any
    #: Set by :meth:`bind_tools`; the OpenAI-format definitions offered on every call.
    bound_tools: list[ToolSchema] | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return f"consilium-{self.provider.name}"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"provider": self.provider.name, "model": self.provider.model}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        del tool_choice, kwargs  # the provider protocol has no forced-choice parameter
        schemas = [convert_to_openai_tool(tool) for tool in tools]
        return self.model_copy(update={"bound_tools": schemas})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("ProviderChatModel is async-only; use ainvoke")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        caller = kwargs.get("caller")
        response = await self.provider.chat(
            [to_provider_message(message) for message in messages],
            tools=self.bound_tools or None,
            tracer=self.tracer_for(),
            caller=caller,
        )
        return ChatResult(generations=[ChatGeneration(message=from_provider_response(response))])


def to_provider_message(message: BaseMessage) -> Message:
    """LangChain message -> the provider-neutral ``Message``."""
    content = message.content if isinstance(message.content, str) else _flatten(message.content)
    if isinstance(message, SystemMessage):
        return Message(role="system", content=content)
    if isinstance(message, HumanMessage):
        return Message(role="user", content=content)
    if isinstance(message, AIMessage):
        return Message(
            role="assistant",
            content=content or None,
            tool_calls=[
                ToolCall(id=str(call["id"]), name=call["name"], arguments=dict(call["args"]))
                for call in message.tool_calls
            ],
        )
    if isinstance(message, ToolMessage):
        return Message(
            role="tool", content=content, tool_call_id=message.tool_call_id, name=message.name
        )
    raise TypeError(f"unsupported message type {type(message).__name__}")


def from_provider_response(response: LLMResponse) -> AIMessage:
    """The provider's reply as an ``AIMessage`` with LangChain-shaped tool calls and usage."""
    return AIMessage(
        content=response.content or "",
        tool_calls=[
            {"id": call.id, "name": call.name, "args": dict(call.arguments), "type": "tool_call"}
            for call in response.tool_calls
        ],
        response_metadata={
            "provider": response.provider,
            "model": response.model,
            "stop_reason": response.stop_reason,
            "latency_ms": response.latency_ms,
        },
        usage_metadata={
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        },
    )


def _flatten(blocks: Sequence[str | dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(json.dumps(block, sort_keys=True))
    return "".join(parts)
