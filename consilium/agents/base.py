"""Agents layer: the specialist base class.

All three specialists are the same object with a different system prompt and a different
permitted-skill list.  That is the design, not an accident of implementation:

**All three agents are registered with all seven skills; ``policy.yaml`` is what narrows them.**
The alternative -- wiring each agent to a hard-coded tool list in its constructor -- puts the
specialization in two places, the prompt and the wiring, which then disagree.  It also makes the
safety policy advisory: a policy file that says an agent may not call ``deep_research`` means
nothing if the agent's tool list was never derived from it.  Here the narrowing happens once, at
construction, and an unknown skill name in the policy raises rather than silently removing a tool.

**The registry is narrowed, and the loop checks again.**  Only the permitted schemas are offered to
the model, so a compliant model cannot ask for anything else; the loop refuses an unpermitted name
anyway, because "the model cannot ask" is a property of the prompt and the refusal is a property of
the code.  Phase 7 attaches a ``safety`` violation event to that same refusal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from consilium.agents.loop import ReActLoop
from consilium.llm.base import LLMProvider, Message
from consilium.safety.policy import Policy
from consilium.skills.base import SkillContext, SkillResult
from consilium.skills.registry import SkillRegistry


@dataclass(frozen=True)
class AgentResult:
    """One specialist's contribution to a turn."""

    agent: str
    answer: str
    sources: tuple[str, ...]
    tool_results: tuple[SkillResult, ...]
    iterations: int
    forced: bool

    @property
    def tool_calls_used(self) -> int:
        return len(self.tool_results)


class BaseAgent:
    """A specialist: a system prompt, a permitted-skill list, and the shared ReAct loop."""

    #: Matches ``agent:[a-z][a-z0-9_]*`` in the trace's caller pattern, because it becomes one.
    name: ClassVar[str] = "base"
    system_prompt: ClassVar[str] = ""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: SkillRegistry,
        policy: Policy,
        loop: ReActLoop | None = None,
    ) -> None:
        # Narrowing here rather than at call time means an agent whose policy names a skill the
        # registry does not have cannot be constructed at all.  `subset` raises on an unknown name;
        # the alternative -- skipping it -- would give the agent one fewer tool than its author
        # intended, silently, for the lifetime of the process.
        self.permitted: tuple[str, ...] = policy.permitted_skills(self.name)
        self.registry = registry.subset(list(self.permitted))
        self.description = policy.description(self.name)
        self.loop = loop or ReActLoop(provider=provider, registry=self.registry)

    async def answer(
        self,
        question: str,
        *,
        ctx: SkillContext,
        history: Sequence[Message] = (),
        objective: str | None = None,
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
    ) -> AgentResult:
        """Answer ``question``, optionally narrowed to the ``objective`` the planner assigned.

        ``ctx`` must carry ``agent=self.name``; :meth:`for_turn` builds one that does.  The
        objective, when present, is prepended to the question rather than merged into the system
        prompt: the system prompt is a stable statement of what this agent is, and rewriting it per
        subtask would make two turns of the same agent incomparable in the trace.
        """
        prompt = question if objective is None else f"{objective}\n\nUser's question: {question}"
        result = await self.loop.run(
            system_prompt=self.system_prompt,
            question=prompt,
            ctx=ctx,
            permitted=self.permitted,
            history=history,
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
        )
        return AgentResult(
            agent=self.name,
            answer=result.answer,
            sources=result.sources,
            tool_results=tuple(result.tool_results),
            iterations=result.iterations,
            forced=result.forced,
        )

    def for_turn(self, ctx: SkillContext) -> SkillContext:
        """Return ``ctx`` re-labelled for this agent.

        The caller assembles one context per turn holding the retriever and the tables; each agent
        stamps its own name onto a copy so that ``tool_call.agent`` and ``llm_call.caller`` agree
        without the caller having to remember to set it.
        """
        if ctx.agent == self.name:
            return ctx
        return SkillContext(
            retriever=ctx.retriever,
            red_flags=ctx.red_flags,
            symptoms=ctx.symptoms,
            documents=ctx.documents,
            tracer=ctx.tracer,
            agent=self.name,
        )
