"""ADK layer: the existing skills, validator and repair, offered as ADK tools.

**Adapters, not reimplementations.**  Every tool here calls the same object the legacy ReAct loop
calls -- :meth:`SkillRegistry.run` for the seven skills, :meth:`PolicyValidator.check_output` and
:meth:`OutputRepair.apply` for the two safety functions -- with the same per-turn
:class:`SkillContext`, so the ``tool_call``, ``retrieval`` and ``safety`` events those objects emit
are the events the eval harness already reads.  Nothing is traced twice and nothing is traced by a
second code path.

**Schemas are derived, never restated.**  A :class:`SkillTool`'s function declaration is
``Skill.parameters_schema()`` -- the same JSON schema the OpenAI tool definitions and the MCP
``inputSchema`` are built from -- passed to Gemini as ``parameters_json_schema``.  ADK's
``FunctionTool`` would have derived a schema from a synthesized Python signature instead, and that
read-back drops ``description`` and ``additionalProperties: false`` (see ``consilium/mcp_server.py``
for the same decision made for MCP).

**The observation the model sees is the legacy observation.**  ``SkillResult.to_observation()`` is
returned verbatim under ADK's conventional ``result`` key, so a Gemini worker reads exactly the
string a GPT worker reads: one renderer, three consumers (loop, MCP, ADK).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from consilium.safety.red_flags import RedFlagTable
from consilium.safety.repair import OutputRepair
from consilium.safety.validator import PolicyValidator
from consilium.skills.base import SkillContext, SkillResult
from consilium.skills.registry import SkillRegistry

#: Builds the :class:`SkillContext` a tool call runs under.  A callable rather than a value so that
#: the context -- which carries the turn's tracer and the calling agent's name -- is the one for
#: *this* turn even though the tool object is constructed once per agent.
ContextProvider = Callable[[], SkillContext]

#: Called with every result a skill produced, so the worker's ``AgentResult`` can carry the same
#: ``tool_results`` the legacy ``LoopResult`` carries.
ResultSink = Callable[[SkillResult], None]


class SkillTool(BaseTool):
    """One registered skill, as an ADK tool."""

    def __init__(
        self,
        registry: SkillRegistry,
        name: str,
        *,
        context: ContextProvider,
        on_result: ResultSink | None = None,
    ) -> None:
        skill = registry.get(name)
        super().__init__(name=skill.name, description=skill.description)
        self._registry = registry
        self._schema = skill.parameters_schema()
        self._context = context
        self._on_result = on_result

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=self._schema,
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:  # noqa: ARG002
        """Validate, run, time and trace the skill -- :meth:`SkillRegistry.run`, on a thread.

        On a worker thread for the reason :meth:`SkillRegistry.execute` gives: skills are
        synchronous CPU work, and three Gemini workers dispatched concurrently must overlap.
        """
        result = await asyncio.to_thread(self._registry.run, self.name, args, self._context())
        if self._on_result is not None:
            self._on_result(result)
        return {"result": result.to_observation()}


def skill_tools(
    registry: SkillRegistry,
    names: Sequence[str],
    *,
    context: ContextProvider,
    on_result: ResultSink | None = None,
) -> list[BaseTool]:
    """The permitted skills of one agent, as ADK tools, in policy order."""
    return [SkillTool(registry, name, context=context, on_result=on_result) for name in names]


def safety_tools(
    validator: PolicyValidator,
    repair: OutputRepair,
    red_flags: RedFlagTable,
    *,
    context: ContextProvider,
) -> list[BaseTool]:
    """``validate_output`` and ``repair_output`` as ADK tools.

    These wrap the two safety functions so that an ADK-native deployment (``adk web``, an Agent
    Engine export) can run them inside the agent graph.  **The measured turn path does not call
    them from an agent.**  There, :func:`consilium.runtime.run_turn` applies the validator and the
    repair at the turn boundary, in code, exactly as it does for the legacy orchestrator -- a guard
    that a model has to remember to call is a request, not a constraint, and the repair rate is a
    reported metric that must mean the same thing on both paths.
    """

    def validate_output(answer: str, question: str) -> dict[str, Any]:
        """Check an answer against the output policy; return the violated rule names."""
        ctx = context()
        assessment = red_flags.assess(question)
        violations = validator.check_output(
            answer, assessment=assessment, agent=ctx.agent, tracer=ctx.tracer
        )
        return {"violations": [violation.rule for violation in violations]}

    def repair_output(answer: str, question: str) -> dict[str, Any]:
        """Apply the output policy's repairs to an answer and return the delivered text."""
        ctx = context()
        assessment = red_flags.assess(question)
        violations = validator.check_output(
            answer, assessment=assessment, agent=ctx.agent, tracer=ctx.tracer
        )
        repaired = repair.apply(answer, violations, agent=ctx.agent, tracer=ctx.tracer)
        return {"answer": repaired.answer, "repairs": list(repaired.repairs)}

    return [FunctionTool(validate_output), FunctionTool(repair_output)]
