"""The registry: discovery, derived tool schemas, and the four ways a tool call can fail.

The failure tests carry most of the weight.  A skill layer that works when everything is correct is
easy; what decides whether an agent survives a bad turn is what happens when the model names a tool
that does not exist, sends arguments that do not validate, or triggers a bug -- and all three have
to arrive back as a traced `ok=False` rather than as an exception.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from consilium.llm.base import ToolSchema
from consilium.skills import (
    SKILL_NAMES,
    SkillContext,
    SkillError,
    SkillRegistry,
    SkillResult,
    declared_skills,
    skill,
)
from consilium.skills.base import Skill
from consilium.trace import MemorySink, ToolCallEvent, Tracer


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


def _explode(args: _Args, ctx: SkillContext) -> SkillResult:
    raise RuntimeError(f"deliberate failure on {args.value} from {ctx.agent}")


def _echo(args: _Args, ctx: SkillContext) -> SkillResult:
    del ctx
    return SkillResult(skill="echo", ok=True, data={"value": args.value}, sources=("doc-a",))


def _skill(name: str, func: Any, *, requires_retrieval: bool = False) -> Skill:
    return Skill(
        name=name,
        description=f"test skill {name}",
        category="knowledge",
        args_model=_Args,
        func=func,
        requires_retrieval=requires_retrieval,
    )


@pytest.fixture
def toy() -> SkillRegistry:
    return SkillRegistry(
        [
            _skill("echo", _echo),
            _skill("explode", _explode),
            _skill("needs_corpus", _echo, requires_retrieval=True),
        ]
    )


def test_discovery_finds_exactly_the_seven_named_skills(registry: SkillRegistry) -> None:
    assert set(registry.names) == set(SKILL_NAMES)
    assert len(registry) == 7


def test_registry_order_is_sorted_not_incidental(registry: SkillRegistry) -> None:
    """Tool order is part of the prompt, so it must not depend on filesystem iteration order."""
    assert list(registry.names) == sorted(registry.names)


def test_every_declared_skill_has_a_distinct_name() -> None:
    names = [item.name for item in declared_skills()]
    assert len(names) == len(set(names))


def test_tool_schemas_are_derived_from_the_pydantic_models(registry: SkillRegistry) -> None:
    schemas = {schema["function"]["name"]: schema for schema in registry.to_tool_schemas()}
    assert set(schemas) == set(SKILL_NAMES)

    for name, schema in schemas.items():
        assert schema["type"] == "function"
        parameters = schema["function"]["parameters"]
        assert parameters["type"] == "object"
        # `extra="forbid"` on every argument model, surfaced to the provider as
        # additionalProperties: false, so a model that invents a field is told so.
        assert parameters["additionalProperties"] is False
        assert set(parameters["properties"]) == set(registry.get(name).args_model.model_fields), (
            name
        )
        assert schema["function"]["description"]


def test_tool_schema_carries_no_hand_written_json(registry: SkillRegistry) -> None:
    """The schema for one skill equals the schema pydantic generates, minus the class title."""
    generated = registry.get("assess_risk").args_model.model_json_schema()
    generated.pop("title")
    emitted: ToolSchema = registry.to_tool_schemas(["assess_risk"])[0]
    assert emitted["function"]["parameters"] == generated


def test_field_descriptions_reach_the_schema(registry: SkillRegistry) -> None:
    schema = registry.to_tool_schemas(["lookup_disease_code"])[0]
    condition = schema["function"]["parameters"]["properties"]["condition"]
    assert "code root" in condition["description"]


def test_subset_narrows_and_rejects_unknown_names(registry: SkillRegistry) -> None:
    narrowed = registry.subset(["assess_risk", "search_knowledge"])
    assert narrowed.names == ("assess_risk", "search_knowledge")
    with pytest.raises(KeyError, match="unknown skill"):
        registry.subset(["assess_risk", "not_a_skill"])


def test_duplicate_names_are_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="duplicate skill name"):
        SkillRegistry([_skill("echo", _echo), _skill("echo", _echo)])


def test_successful_call_is_timed_and_traced(
    toy: SkillRegistry, tracer: Tracer, memory_sink: MemorySink
) -> None:
    result = toy.run("echo", {"value": 7}, SkillContext(tracer=tracer, agent="diagnostic"))

    assert result.ok is True
    assert result.data == {"value": 7}
    assert result.latency_ms >= 0.0

    (event,) = memory_sink.of_type(ToolCallEvent)
    assert event.agent == "diagnostic"
    assert event.skill == "echo"
    assert event.args == {"value": 7}
    assert event.ok is True
    assert event.source_doc_ids == ["doc-a"]


def test_unknown_skill_returns_a_failure_and_still_traces(
    toy: SkillRegistry, tracer: Tracer, memory_sink: MemorySink
) -> None:
    result = toy.run("hallucinated", {"value": 1}, SkillContext(tracer=tracer))

    assert result.ok is False
    assert result.error is not None and "unknown skill" in result.error
    (event,) = memory_sink.of_type(ToolCallEvent)
    assert event.ok is False
    assert event.skill == "hallucinated"


def test_invalid_arguments_return_a_failure_naming_the_field(
    toy: SkillRegistry, tracer: Tracer, memory_sink: MemorySink
) -> None:
    result = toy.run("echo", {"value": "not a number"}, SkillContext(tracer=tracer))

    assert result.ok is False
    assert result.error is not None
    assert "invalid arguments" in result.error
    assert "value" in result.error
    assert memory_sink.of_type(ToolCallEvent)[0].ok is False


def test_extra_arguments_are_rejected_rather_than_ignored(toy: SkillRegistry) -> None:
    result = toy.run("echo", {"value": 1, "invented": True}, SkillContext())
    assert result.ok is False
    assert result.error is not None and "invented" in result.error


def test_a_raising_skill_becomes_a_failed_result_not_an_exception(
    toy: SkillRegistry, tracer: Tracer, memory_sink: MemorySink
) -> None:
    result = toy.run("explode", {"value": 3}, SkillContext(tracer=tracer, agent="research"))

    assert result.ok is False
    assert result.error == "RuntimeError: deliberate failure on 3 from research"
    assert memory_sink.of_type(ToolCallEvent)[0].ok is False


def test_retrieval_backed_skill_fails_cleanly_when_retrieval_is_off(toy: SkillRegistry) -> None:
    """`RunConfig.retrieval=False` is an ablation row, so it must be a measurable outcome."""
    result = toy.run("needs_corpus", {"value": 1}, SkillContext(retriever=None))

    assert result.ok is False
    assert result.error is not None and "retrieval is disabled" in result.error


async def test_execute_runs_the_same_call_on_a_worker_thread(toy: SkillRegistry) -> None:
    result = await toy.execute("echo", {"value": 11}, SkillContext(agent="consultation"))
    assert result.ok is True
    assert result.data == {"value": 11}


async def test_execute_does_not_block_the_event_loop() -> None:
    """Two skill calls dispatched together must overlap, not take turns.

    This is the property the parallel router depends on: if skills held the event loop, three
    workers under `asyncio.gather` would serialize and the parallel-versus-single latency
    comparison would be measuring the harness.
    """
    import asyncio
    import time

    started = asyncio.Event()

    def _slow(args: _Args, ctx: SkillContext) -> SkillResult:
        del ctx
        time.sleep(0.05)
        return SkillResult(skill="slow", ok=True, data={"value": args.value})

    registry = SkillRegistry([_skill("slow", _slow)])
    ctx = SkillContext()

    async def _ping() -> float:
        await started.wait()
        return time.perf_counter()

    async def _call() -> SkillResult:
        started.set()
        return await registry.execute("slow", {"value": 1}, ctx)

    start = time.perf_counter()
    _, ping = await asyncio.gather(_call(), _ping())
    # The ping resolves while the skill is still sleeping, which can only happen if the sleep is
    # off the event loop.
    assert ping - start < 0.04


def test_declaring_a_skill_without_an_args_annotation_is_an_error() -> None:
    with pytest.raises(SkillError, match="annotated `args` parameter"):

        @skill(name="broken_no_args", description="d", category="knowledge")
        def _broken(args, ctx: SkillContext) -> SkillResult:  # type: ignore[no-untyped-def]
            del args, ctx
            return SkillResult(skill="broken_no_args", ok=True)


def test_declaring_a_skill_with_a_non_model_args_annotation_is_an_error() -> None:
    with pytest.raises(SkillError, match="pydantic model"):

        @skill(name="broken_args_type", description="d", category="knowledge")
        def _broken(args: int, ctx: SkillContext) -> SkillResult:
            del args, ctx
            return SkillResult(skill="broken_args_type", ok=True)


def test_declaring_the_same_name_twice_is_an_error() -> None:
    with pytest.raises(SkillError, match="declared twice"):

        @skill(name="assess_risk", description="d", category="triage")
        def _duplicate(args: _Args, ctx: SkillContext) -> SkillResult:
            del args, ctx
            return SkillResult(skill="assess_risk", ok=True)


def test_observation_rendering_is_compact_json_or_a_plain_error() -> None:
    ok = SkillResult(skill="echo", ok=True, data={"a": 1}, sources=("doc-a",))
    assert ok.to_observation() == '{"data":{"a":1},"sources":["doc-a"]}'

    failed = SkillResult.failure("echo", "no")
    assert failed.to_observation() == "ERROR: no"


def test_registry_is_a_container_of_its_skills(registry: SkillRegistry) -> None:
    assert len(registry) == 7
    assert "assess_risk" in registry
    assert "not_a_skill" not in registry
    assert {item.name for item in registry} == set(SKILL_NAMES)


def test_require_retriever_names_the_skill_it_was_called_for() -> None:
    """Unreachable through the registry, which refuses first -- so it is tested directly."""
    from consilium.skills import require_retriever

    with pytest.raises(SkillError, match="deep_research was invoked without a retriever"):
        require_retriever(SkillContext(), "deep_research")
