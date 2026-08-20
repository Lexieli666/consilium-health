"""The blackboard: isolation between workers, and a lifecycle reconstructable from the trace.

"Workers read only their own assignment and write only their own result" is a claim about the API,
not about discipline, so the tests are about what a `SubtaskHandle` can and cannot reach.
"""

from __future__ import annotations

import asyncio

import pytest

from consilium.agents.base import AgentResult
from consilium.router import Blackboard, Subtask
from consilium.trace import BlackboardEvent, MemorySink, Tracer


def _subtasks(*agents: str) -> list[Subtask]:
    return [
        Subtask(subtask_id=f"{index}-{agent}", agent=agent, objective="do it", why="because")
        for index, agent in enumerate(agents, start=1)
    ]


def _result(agent: str, answer: str = "done") -> AgentResult:
    return AgentResult(
        agent=agent, answer=answer, sources=("doc-a",), tool_results=(), iterations=1, forced=False
    )


async def test_assignment_returns_one_handle_per_subtask_in_plan_order() -> None:
    board = Blackboard()
    handles = await board.assign(_subtasks("diagnostic", "research"))

    assert [handle.agent for handle in handles] == ["diagnostic", "research"]
    assert [record.status for record in board.records] == ["assigned", "assigned"]


async def test_a_handle_exposes_only_its_own_assignment() -> None:
    """There is no accessor for anyone else's subtask, so no worker can read another's."""
    board = Blackboard()
    first, second = await board.assign(_subtasks("diagnostic", "research"))

    assert first.subtask.agent == "diagnostic"
    assert second.subtask.agent == "research"

    public = {name for name in dir(first) if not name.startswith("_")}
    assert public == {"subtask", "agent", "started", "completed", "failed", "timed_out"}


async def test_the_lifecycle_is_recorded_in_order() -> None:
    board = Blackboard()
    (handle,) = await board.assign(_subtasks("consultation"))

    await handle.started()
    await handle.completed(_result("consultation"))

    assert [entry.event for entry in board.log] == ["assigned", "started", "completed"]
    assert board.completed()[0].result is not None
    assert board.unfinished() == ()


async def test_every_transition_is_mirrored_to_the_trace(memory_sink: MemorySink) -> None:
    """The lifecycle of a turn must be reconstructable from the trace alone."""
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)
    board = Blackboard(tracer=tracer)
    (handle,) = await board.assign(_subtasks("research"))

    await handle.started()
    await handle.failed("boom")

    events = memory_sink.of_type(BlackboardEvent)
    assert [event.event for event in events] == ["assigned", "started", "failed"]
    assert {event.subtask_id for event in events} == {"1-research"}
    assert {event.agent for event in events} == {"research"}


async def test_a_failed_subtask_is_unfinished_and_carries_its_error() -> None:
    board = Blackboard()
    (handle,) = await board.assign(_subtasks("research"))

    await handle.failed("RuntimeError: upstream is down")

    (record,) = board.unfinished()
    assert record.status == "failed"
    assert record.error == "RuntimeError: upstream is down"
    assert board.completed() == ()


async def test_a_timed_out_subtask_is_distinguished_from_a_failed_one() -> None:
    board = Blackboard()
    (handle,) = await board.assign(_subtasks("diagnostic"))

    await handle.timed_out("exceeded the 90s turn deadline")

    (record,) = board.unfinished()
    assert record.status == "timeout"


async def test_a_completed_record_needs_both_the_status_and_the_result() -> None:
    board = Blackboard()
    (handle,) = await board.assign(_subtasks("consultation"))
    await handle.started()

    assert board.completed() == ()
    await handle.completed(_result("consultation"))
    assert len(board.completed()) == 1


async def test_duplicate_subtask_ids_are_refused() -> None:
    board = Blackboard()
    duplicate = _subtasks("research") * 2

    with pytest.raises(ValueError, match="duplicate subtask_id"):
        await board.assign(duplicate)


async def test_concurrent_workers_do_not_lose_each_other_s_writes() -> None:
    """The board is shared across an `asyncio.gather`, so its mutations are lock-guarded."""
    board = Blackboard()
    handles = await board.assign(_subtasks(*[f"consultation{n}" for n in range(20)]))

    async def _work(index: int) -> None:
        handle = handles[index]
        await handle.started()
        await asyncio.sleep(0)
        await handle.completed(_result(handle.agent, answer=f"answer {index}"))

    await asyncio.gather(*(_work(index) for index in range(len(handles))))

    assert len(board.completed()) == 20
    answers = {record.result.answer for record in board.completed() if record.result is not None}
    assert len(answers) == 20
    assert len(board.log) == 60  # assigned + started + completed for each
