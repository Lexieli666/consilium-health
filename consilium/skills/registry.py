"""Skills layer: the registry -- discovery, tool schemas, and the one place a skill is invoked.

The registry is the boundary between the model's untrusted tool call and the typed function that
serves it.  Everything that can go wrong at that boundary is handled here, once, rather than in
seven implementations:

* the model names a tool that does not exist;
* the model passes arguments that fail validation;
* the skill needs a retriever and the run configuration switched retrieval off;
* the skill raises.

All four come back as ``SkillResult(ok=False, ...)`` and all four emit a ``tool_call`` event with
``ok=False`` and the reason.  A tool call that failed is data; a tool call that killed the turn is
an outage.

**Discovery is by import, and the order is sorted rather than incidental.**  ``to_tool_schemas()``
feeds the prompt, so tool order is part of the prompt, so an incidental order derived from
filesystem iteration would make prompt-token counts differ between machines.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from consilium.llm.base import ToolSchema
from consilium.log import get_logger
from consilium.skills.base import (
    Skill,
    SkillContext,
    SkillResult,
    declared_skills,
)
from consilium.trace import stopwatch

log = get_logger(__name__)

#: Modules under ``consilium.skills`` that hold no skills.  Named explicitly so that discovery can
#: assert it found something in every other module, which is what turns "I forgot to decorate the
#: function" from a silently missing tool into an import-time error.
_NON_SKILL_MODULES = frozenset({"base", "registry", "symptom_map"})


class SkillRegistry:
    """The set of skills the system can offer to a model."""

    def __init__(self, skills: Iterable[Skill]) -> None:
        ordered = sorted(skills, key=lambda item: item.name)
        seen: set[str] = set()
        for item in ordered:
            if item.name in seen:
                raise ValueError(f"duplicate skill name {item.name!r} in the registry")
            seen.add(item.name)
        self._skills: dict[str, Skill] = {item.name: item for item in ordered}

    @classmethod
    def discover(cls) -> SkillRegistry:
        """Import every skill module here, then build a registry from what they declared."""
        import consilium.skills as package

        for module in sorted(info.name for info in pkgutil.iter_modules(package.__path__)):
            if module in _NON_SKILL_MODULES:
                continue
            importlib.import_module(f"{package.__name__}.{module}")
        return cls(declared_skills())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            known = ", ".join(self.names)
            raise KeyError(f"unknown skill {name!r}; registered: {known}") from None

    def subset(self, names: Sequence[str]) -> SkillRegistry:
        """A registry holding only ``names``.  Raises if a name is not registered.

        Used by the agent layer to narrow the seven skills down to the list ``policy.yaml`` permits
        for one agent.  Raising on an unknown name is deliberate: a typo in the policy file would
        otherwise silently give an agent one fewer tool than its author intended.
        """
        return SkillRegistry([self.get(name) for name in names])

    def to_tool_schemas(self, names: Sequence[str] | None = None) -> list[ToolSchema]:
        """OpenAI-format tool definitions, derived from the skills' Pydantic argument models."""
        chosen = self.names if names is None else tuple(names)
        return [
            {
                "type": "function",
                "function": {
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.parameters_schema(),
                },
            }
            for item in (self.get(name) for name in chosen)
        ]

    def run(self, name: str, args: Mapping[str, Any], ctx: SkillContext) -> SkillResult:
        """Validate, invoke, time and trace one skill call.  Never raises."""
        with stopwatch() as elapsed_ms:
            result = self._invoke(name, args, ctx)
        result = result.with_latency(elapsed_ms())

        if ctx.tracer is not None:
            ctx.tracer.tool_call(
                agent=ctx.agent,
                skill=name,
                args=dict(args),
                ok=result.ok,
                error=result.error,
                latency_ms=result.latency_ms,
                source_doc_ids=list(result.sources),
                transport=ctx.transport,
            )
        return result

    async def execute(self, name: str, args: Mapping[str, Any], ctx: SkillContext) -> SkillResult:
        """Await :meth:`run` on a worker thread.

        Skills are synchronous CPU work.  Calling them straight from the coroutine would hold the
        event loop for the duration, which is invisible on the single-agent path and destroys the
        parallel one: three workers dispatched by ``asyncio.gather`` would take turns instead of
        overlapping, and the parallel-versus-single latency comparison in docs/EVALUATION.md would
        be measuring the harness rather than the architecture.
        """
        return await asyncio.to_thread(self.run, name, args, ctx)

    def _invoke(self, name: str, args: Mapping[str, Any], ctx: SkillContext) -> SkillResult:
        try:
            declared = self.get(name)
        except KeyError as exc:
            return SkillResult.failure(name, str(exc))

        if declared.requires_retrieval and ctx.retriever is None:
            return SkillResult.failure(
                name, f"{name} needs the retrieval corpus, and retrieval is disabled for this run"
            )

        try:
            parsed: BaseModel = declared.args_model.model_validate(dict(args))
        except ValidationError as exc:
            return SkillResult.failure(name, _explain(exc))

        try:
            return declared.func(parsed, ctx)
        except Exception as exc:  # a skill bug must not end the turn; see the module docstring
            log.exception("skill.failed", skill=name, agent=ctx.agent)
            return SkillResult.failure(name, f"{type(exc).__name__}: {exc}")

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def __contains__(self, name: object) -> bool:
        return name in self._skills


def _explain(exc: ValidationError) -> str:
    """Flatten a pydantic error into one line the model can act on.

    The observation goes back to the model, which then gets one more attempt within its tool budget.
    Pydantic's default rendering is multi-line and repeats the model class name on every error; what
    the model needs is the field and what was wrong with it.
    """
    parts = [
        f"{'.'.join(str(location) for location in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()
    ]
    return "invalid arguments -- " + "; ".join(parts)
