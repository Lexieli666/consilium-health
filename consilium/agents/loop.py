"""Agents layer: the ReAct engine all three specialists share.

Think -> Act -> Observe, with a budget the loop enforces rather than requests.

**Two budgets, and only one of them binds in practice.**  ``max_tool_calls`` (default 2) is the
constraint that decides how much retrieval an answer is grounded in and how many tokens the turn
costs.  ``max_iterations`` (default 3) is an independent guard against a model that keeps producing
prose without ever calling a tool -- a failure ``max_tool_calls`` cannot catch, because it never
increments.  Defending both as if both bind would be dishonest; they exist for different failures.

**The budget is enforced here, not asked for in the prompt.**  A prompt that says "use at most two
tools" is a request; a loop that stops offering tool schemas is a constraint.  The difference shows
up in the tool-call distribution, which is a reported metric.

**The user always gets an answer.**  When the tool budget runs out, the next call is made with tools
disabled and is attributed to ``forced_answer`` in the trace, so the cost of the forced turn is
visible as its own bucket in tokens-per-turn.  If iterations run out with tool calls still pending,
the same forced call happens after the loop.  If the provider returns nothing usable even then, a
fixed sentence is delivered -- an empty answer is a worse outcome than an honest one.

**Tool calls the model requests beyond its budget are refused without a ``tool_call`` event.**  They
were not executed, and counting them would inflate the tool-call distribution that
``full_budget_6`` exists to measure honestly.  The model still gets a ``tool`` message for each, as
the API requires, saying the budget is spent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from consilium.llm.base import LLMProvider, LLMResponse, Message, ToolCall
from consilium.log import get_logger
from consilium.skills.base import SkillContext, SkillResult
from consilium.skills.registry import SkillRegistry

log = get_logger(__name__)

DEFAULT_MAX_ITERATIONS = 3
DEFAULT_MAX_TOOL_CALLS = 2

#: Delivered when even the forced call produced no text.  Not an apology and not an invented answer.
EMPTY_ANSWER_FALLBACK = (
    "I was not able to produce an answer for this question. If this concerns a symptom that is "
    "severe, worsening, or new and unexplained, seek medical advice."
)

#: The observation handed back for a tool call the model requested after its budget was spent.
BUDGET_EXHAUSTED_OBSERVATION = (
    "ERROR: tool-call budget exhausted for this turn; answer from what has already been gathered"
)

#: The observation handed back when an agent calls a skill its policy does not permit.  Phase 7
#: attaches a ``safety`` violation event to this same branch; the refusal itself lives here so that
#: the loop is never able to execute an unpermitted skill even if the validator is absent.
NOT_PERMITTED_OBSERVATION = "is not permitted for this agent"


@dataclass
class LoopResult:
    """What one agent turn produced."""

    answer: str
    messages: list[Message]
    tool_results: list[SkillResult] = field(default_factory=list)
    iterations: int = 0
    forced: bool = False

    @property
    def sources(self) -> tuple[str, ...]:
        """Every ``doc_id`` cited by a successful tool call, in first-seen order."""
        seen: dict[str, None] = {}
        for result in self.tool_results:
            if result.ok:
                for doc_id in result.sources:
                    seen.setdefault(doc_id, None)
        return tuple(seen)

    @property
    def tool_calls_used(self) -> int:
        return len(self.tool_results)


class ReActLoop:
    """Runs one agent's think/act/observe cycle against a provider."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: SkillRegistry,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ) -> None:
        if max_iterations < 1:
            raise ValueError(f"max_iterations must be >= 1; got {max_iterations}")
        if max_tool_calls < 0:
            raise ValueError(f"max_tool_calls must be >= 0; got {max_tool_calls}")
        self.provider = provider
        self.registry = registry
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls

    async def run(
        self,
        *,
        system_prompt: str,
        question: str,
        ctx: SkillContext,
        permitted: Sequence[str],
        history: Sequence[Message] = (),
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
    ) -> LoopResult:
        """Answer ``question`` within the budget.

        The agent's identity comes from ``ctx.agent`` and is not a separate parameter.  It labels
        both the ``llm_call.caller`` and the ``tool_call.agent`` of every event this turn emits, and
        two parameters that had to agree would eventually not, leaving one turn's events split
        across two names.

        ``max_iterations`` and ``max_tool_calls`` override the constructor defaults per call, which
        is what ``RunConfig`` needs: ``full_budget_6`` differs from ``full`` in exactly this number
        and must not require a second loop instance to express.
        """
        iterations_budget = self.max_iterations if max_iterations is None else max_iterations
        tool_budget = self.max_tool_calls if max_tool_calls is None else max_tool_calls
        tools = self.registry.to_tool_schemas(list(permitted))

        messages: list[Message] = [
            Message(role="system", content=system_prompt),
            *history,
            Message(role="user", content=question),
        ]
        result = LoopResult(answer="", messages=messages)

        while result.iterations < iterations_budget:
            remaining = tool_budget - result.tool_calls_used
            offer_tools = remaining > 0
            # `forced_answer` is reserved for a call made *because* a budget ran out.  A run
            # configured with max_tool_calls=0 -- the baseline_llm ablation -- never offered tools
            # in the first place, so its calls stay attributed to the agent.
            forced = tool_budget > 0 and not offer_tools
            response = await self.provider.chat(
                messages,
                tools=tools if offer_tools else None,
                tracer=ctx.tracer,
                caller="forced_answer" if forced else f"agent:{ctx.agent}",
            )
            result.iterations += 1

            # `not offer_tools` short-circuits a provider that returns tool calls after being
            # offered no tools.  Without it the loop would append an assistant turn, refuse every
            # call for lack of budget, and spin until max_iterations -- burning the turn on a
            # provider bug rather than answering.
            if not offer_tools or not response.tool_calls:
                result.answer = response.content or ""
                result.forced = forced
                if result.answer:
                    return result
                break

            messages.append(_assistant_turn(response))
            await self._observe(response.tool_calls, remaining, ctx, permitted, result, messages)

        if not result.answer:
            await self._force_answer(messages, ctx, result)
        return result

    async def _observe(
        self,
        calls: Sequence[ToolCall],
        remaining: int,
        ctx: SkillContext,
        permitted: Sequence[str],
        result: LoopResult,
        messages: list[Message],
    ) -> None:
        """Execute the requested tool calls within what is left of the budget."""
        executed = 0
        for call in calls:
            if executed >= remaining:
                messages.append(
                    Message(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=BUDGET_EXHAUSTED_OBSERVATION,
                    )
                )
                continue

            executed += 1
            if call.name not in permitted:
                skill_result = SkillResult.failure(
                    call.name, f"{call.name} {NOT_PERMITTED_OBSERVATION}"
                )
                log.warning("agent.tool_not_permitted", agent=ctx.agent, skill=call.name)
            else:
                skill_result = await self.registry.execute(call.name, call.arguments, ctx)

            result.tool_results.append(skill_result)
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=skill_result.to_observation(),
                )
            )

    async def _force_answer(
        self, messages: list[Message], ctx: SkillContext, result: LoopResult
    ) -> None:
        """One final call with tools disabled, so the turn always ends in an answer."""
        response = await self.provider.chat(
            messages, tools=None, tracer=ctx.tracer, caller="forced_answer"
        )
        result.iterations += 1
        result.forced = True
        result.answer = response.content or EMPTY_ANSWER_FALLBACK
        if not response.content:
            log.warning("agent.empty_forced_answer", agent=ctx.agent)


def _assistant_turn(response: LLMResponse) -> Message:
    """The assistant message recording the model's tool-call request.

    Included in the transcript even when the model produced no prose alongside the calls, because
    the next request has to show the model what it asked for -- a tool result with no matching
    request is rejected by every provider that accepts tool calls at all.
    """
    return Message(role="assistant", content=response.content, tool_calls=list(response.tool_calls))
