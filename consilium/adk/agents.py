"""ADK layer: the planner, the three specialists and the synthesizer as ADK ``LlmAgent``s.

Same prompts, same permitted-skill lists, same budgets as ``consilium/agents/`` and
``consilium/router/``; what changes is the engine underneath -- Google ADK's LLM flow on Vertex AI
Gemini instead of the in-repo ReAct loop on an OpenAI-format provider.

**Trace compatibility is implemented with ADK callbacks, and it is the point of this module.**
Every ``llm_call`` the legacy path writes, this path writes too, with the same ``caller`` label
(``planner``, ``agent:<name>``, ``forced_answer``, ``synthesizer``), the same token and latency
fields and the same ``tools_offered`` list -- from ``before_model_callback`` /
``after_model_callback``.  Every ``tool_call`` and ``retrieval`` event comes from the registry
itself, because :class:`consilium.adk.tools.SkillTool` calls it.  ``safety`` violations for a
tool the policy does not permit come from the same :class:`PolicyValidator` the loop uses, called
from ``before_tool_callback``.  The eval harness therefore runs on an ADK trace unchanged.

**The budgets are enforced in code, exactly as the loop enforces them.**  ``before_tool_callback``
refuses a call past ``max_tool_calls`` with the loop's ``BUDGET_EXHAUSTED_OBSERVATION`` and no
``tool_call`` event; ``before_model_callback`` withholds the tool declarations once the budget is
spent -- or once ``max_iterations`` model calls have happened -- and labels that call
``forced_answer``.  A Gemini worker cannot retrieve more than a GPT worker could, which is what
makes the two runs comparable.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from consilium.adk.tools import skill_tools
from consilium.agents.loop import BUDGET_EXHAUSTED_OBSERVATION, NOT_PERMITTED_OBSERVATION
from consilium.agents.prompts import CONSULTATION, DIAGNOSTIC, RESEARCH
from consilium.config import RunConfig
from consilium.log import get_logger
from consilium.router import planner as legacy_planner
from consilium.router import synthesizer as legacy_synthesizer
from consilium.safety.policy import Policy
from consilium.safety.validator import PolicyValidator
from consilium.skills.base import SkillContext, SkillResult
from consilium.skills.registry import SkillRegistry
from consilium.trace import Tracer

log = get_logger(__name__)

#: ``llm_call.provider`` for every ADK call.  Vertex AI is the only backend this path is deployed
#: on; ``eval/pricing.yaml`` keys rates on ``<provider>/<model>``, so the label is load-bearing.
PROVIDER_LABEL = "vertexai"

#: The specialists' system prompts, by the name the policy and the planner use.
SPECIALIST_PROMPTS: dict[str, str] = {
    "consultation": CONSULTATION,
    "diagnostic": DIAGNOSTIC,
    "research": RESEARCH,
}


@dataclass
class WorkerScratch:
    """What one agent run accumulates, for the ``AgentResult`` and the trace.

    One per agent run, held by closure in that run's callbacks.  ADK's session state could carry
    the counters, but it cannot carry the :class:`SkillResult` objects, and splitting the two would
    put one run's bookkeeping in two places.
    """

    caller: str
    model: str
    tool_budget: int
    iteration_budget: int
    tool_results: list[SkillResult] = field(default_factory=list)
    model_calls: int = 0
    executed: int = 0
    forced: bool = False
    #: Set by ``before_model_callback`` when this call goes out with tools withheld.
    _forced_call: bool = False
    _started: float = 0.0
    _tools_offered: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.tool_budget - self.executed

    @property
    def sources(self) -> tuple[str, ...]:
        """Every ``doc_id`` cited by a successful tool call, in first-seen order."""
        seen: dict[str, None] = {}
        for result in self.tool_results:
            if result.ok:
                for doc_id in result.sources:
                    seen.setdefault(doc_id, None)
        return tuple(seen)


def _declared_names(llm_request: LlmRequest) -> list[str]:
    names: list[str] = []
    for tool in llm_request.config.tools or []:
        declarations = getattr(tool, "function_declarations", None) or []
        names.extend(d.name for d in declarations if d.name)
    return names


def _stop_reason(response: LlmResponse) -> str:
    """Map Gemini's finish reason onto the trace's closed ``StopReason`` vocabulary."""
    if response.error_code:
        return "error"
    parts = response.content.parts if response.content is not None else None
    if parts and any(part.function_call is not None for part in parts):
        return "tool_calls"
    reason = response.finish_reason
    if reason is None or reason == types.FinishReason.STOP:
        return "stop"
    if reason == types.FinishReason.MAX_TOKENS:
        return "length"
    if reason in (
        types.FinishReason.SAFETY,
        types.FinishReason.RECITATION,
        types.FinishReason.BLOCKLIST,
        types.FinishReason.PROHIBITED_CONTENT,
        types.FinishReason.SPII,
    ):
        return "content_filter"
    return "other"


def trace_callbacks(scratch: WorkerScratch, tracer: Tracer | None) -> dict[str, Any]:
    """``before_model`` / ``after_model`` callbacks that enforce budgets and write ``llm_call``."""

    def before_model(callback_context: CallbackContext, llm_request: LlmRequest) -> None:  # noqa: ARG001
        scratch.model_calls += 1
        offer_tools = scratch.remaining > 0 and scratch.model_calls <= scratch.iteration_budget
        # `forced_answer` is reserved for a call made *because* a budget ran out.  A run with
        # max_tool_calls=0 never offered tools, so its calls stay attributed to the agent.
        scratch._forced_call = (
            scratch.tool_budget > 0 and not offer_tools and bool(llm_request.config.tools)
        )
        if not offer_tools:
            llm_request.config.tools = None
            llm_request.tools_dict = {}
        scratch._tools_offered = _declared_names(llm_request)
        scratch._started = time.perf_counter()
        return None

    def after_model(callback_context: CallbackContext, llm_response: LlmResponse) -> None:  # noqa: ARG001
        if llm_response.partial:
            return None
        latency_ms = (time.perf_counter() - scratch._started) * 1000.0
        usage = llm_response.usage_metadata
        prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
        # Thinking tokens are billed as output on Vertex AI, so they belong in the completion
        # count a cost-per-turn is computed from.  Leaving them out would understate Gemini.
        completion_tokens = (
            (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0) if usage else 0
        )
        if scratch._forced_call:
            scratch.forced = True
        if tracer is not None:
            tracer.llm_call(
                caller="forced_answer" if scratch._forced_call else scratch.caller,
                provider=PROVIDER_LABEL,
                # The configured model id, not the served `model_version`: it is the key
                # eval/pricing.yaml prices and --max-cost checks before the sweep starts, and a
                # version suffix appearing mid-run would make the cap abort as "unpriced".
                model=scratch.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                tools_offered=scratch._tools_offered,
                stop_reason=_stop_reason(llm_response),
            )
        return None

    return {"before_model_callback": before_model, "after_model_callback": after_model}


def budget_callback(
    scratch: WorkerScratch,
    *,
    permitted: Sequence[str],
    validator: PolicyValidator | None,
    context: SkillContext,
) -> Any:
    """``before_tool`` callback: the loop's budget and permission checks, verbatim in behaviour.

    A call past the budget gets :data:`BUDGET_EXHAUSTED_OBSERVATION` and is not executed and not
    traced, as in the loop.  A call the policy does not permit is refused through the validator --
    which is what emits the ``safety`` violation -- and recorded as a failed result, as in the loop.
    """

    def before_tool(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext) -> Any:  # noqa: ARG001
        if scratch.remaining <= 0:
            return {"result": BUDGET_EXHAUSTED_OBSERVATION}
        scratch.executed += 1
        if validator is not None:
            allowed = validator.check_tool_call(
                agent=context.agent, skill=tool.name, tracer=context.tracer
            ).allowed
        else:
            allowed = tool.name in permitted
        if allowed:
            return None
        failure = SkillResult.failure(tool.name, f"{tool.name} {NOT_PERMITTED_OBSERVATION}")
        scratch.tool_results.append(failure)
        log.warning("adk.tool_not_permitted", agent=context.agent, skill=tool.name)
        return {"result": failure.to_observation()}

    return before_tool


def build_planner(
    *, model: str | BaseLlm, policy: Policy, scratch: WorkerScratch, tracer: Tracer | None
) -> LlmAgent:
    """The planner: the legacy planner's prompt, as an ADK agent that must reply with JSON."""
    planner = legacy_planner.Planner(provider=_NoProvider(), policy=policy)
    instruction = (
        f"{legacy_planner.SYSTEM_PROMPT}\n"
        f"The specialists are:\n{planner.capability_block()}\n\n{legacy_planner.FEW_SHOT}"
    )
    return LlmAgent(
        name="planner",
        description="Decides which specialists answer, and what each should do.",
        model=model,
        instruction=instruction,
        include_contents="none",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        generate_content_config=types.GenerateContentConfig(response_mime_type="application/json"),
        **trace_callbacks(scratch, tracer),
    )


def build_specialist(
    name: str,
    *,
    model: str | BaseLlm,
    registry: SkillRegistry,
    policy: Policy,
    validator: PolicyValidator | None,
    config: RunConfig,
    context: SkillContext,
    scratch: WorkerScratch,
) -> LlmAgent:
    """One specialist: its legacy system prompt and its policy-permitted skills, as ADK tools."""
    permitted = policy.permitted_skills(name)
    tools = skill_tools(
        registry.subset(list(permitted)),
        permitted,
        context=lambda: context,
        on_result=scratch.tool_results.append,
    )
    return LlmAgent(
        name=name,
        description=policy.description(name),
        model=model,
        instruction=SPECIALIST_PROMPTS[name],
        tools=tools if config.max_tool_calls > 0 else [],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        before_tool_callback=budget_callback(
            scratch, permitted=permitted, validator=validator, context=context
        ),
        **trace_callbacks(scratch, context.tracer),
    )


def build_synthesizer(
    *, model: str | BaseLlm, scratch: WorkerScratch, tracer: Tracer | None
) -> LlmAgent:
    """The synthesizer: the legacy merge prompt, as an ADK agent with no tools."""
    return LlmAgent(
        name="synthesizer",
        description="Merges the specialists' answers into one, by fixed precedence.",
        model=model,
        instruction=legacy_synthesizer.SYSTEM_PROMPT,
        include_contents="none",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        **trace_callbacks(scratch, tracer),
    )


class _NoProvider:
    """Stands in for ``LLMProvider`` where only the planner's prompt assembly is wanted.

    The legacy :class:`Planner` is constructed to borrow ``capability_block()`` and ``parse()``;
    its ``plan()`` -- the only method that touches the provider -- is never called on this path.
    """

    name = "none"
    model = "none"

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002  # pragma: no cover
        raise RuntimeError("the ADK planner does not call the legacy provider")

    def stream_chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002  # pragma: no cover
        raise RuntimeError("the ADK planner does not call the legacy provider")
