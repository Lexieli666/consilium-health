"""Router layer: the shared blackboard.

An asyncio-safe store of subtasks, their statuses and their results, with an append-only event log
mirrored to the turn's trace as ``blackboard`` events.

**Workers read only their own assignment and write only their own result, and that is enforced by
the type they are given rather than by convention.**  A worker never sees the :class:`Blackboard`;
it gets a :class:`SubtaskHandle` bound to one ``subtask_id``, whose entire surface is "here is my
assignment" and "here is what happened to me".  There is no method on a handle that can read another
subtask's result, so worker-to-worker communication is not a rule someone has to remember -- it is
absent from the API.

That matters beyond tidiness.  If workers could read each other, the parallel path would stop being
parallel in any meaningful sense: outputs would depend on completion order, and two runs of the same
question could synthesize differently because one worker happened to finish first.  The evaluation
compares parallel turns against single ones, and a nondeterministic merge would be measured as
architecture.

Every state change emits a ``blackboard`` trace event, so the lifecycle of a turn -- who was
assigned, who started, who finished, who timed out -- is reconstructable from the trace alone.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from consilium.log import get_logger
from consilium.router.plan import Subtask
from consilium.trace import BlackboardKind, Tracer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from consilium.agents.base import AgentResult

log = get_logger(__name__)


@dataclass
class SubtaskRecord:
    """One subtask's state on the board."""

    subtask: Subtask
    status: BlackboardKind = "assigned"
    result: AgentResult | None = None
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed" and self.result is not None


@dataclass(frozen=True)
class BlackboardEntry:
    """One line of the append-only log."""

    event: BlackboardKind
    subtask_id: str
    agent: str
    detail: str = ""


class Blackboard:
    """The board itself.  Held by the router; never handed to a worker."""

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        self._tracer = tracer
        self._records: dict[str, SubtaskRecord] = {}
        self._log: list[BlackboardEntry] = []
        self._lock = asyncio.Lock()

    async def assign(self, subtasks: list[Subtask]) -> list[SubtaskHandle]:
        """Put subtasks on the board and return one handle each, in plan order."""
        async with self._lock:
            for subtask in subtasks:
                if subtask.subtask_id in self._records:
                    raise ValueError(f"duplicate subtask_id {subtask.subtask_id!r}")
                self._records[subtask.subtask_id] = SubtaskRecord(subtask=subtask)
                self._append("assigned", subtask.subtask_id, subtask.agent)
        return [SubtaskHandle(self, subtask.subtask_id) for subtask in subtasks]

    async def _transition(
        self,
        subtask_id: str,
        status: BlackboardKind,
        *,
        result: AgentResult | None = None,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            record = self._records[subtask_id]
            record.status = status
            if result is not None:
                record.result = result
            if error is not None:
                record.error = error
            self._append(status, subtask_id, record.subtask.agent, error or "")

    def _append(self, event: BlackboardKind, subtask_id: str, agent: str, detail: str = "") -> None:
        """Append to the log and mirror to the trace.  Called with the lock held."""
        self._log.append(
            BlackboardEntry(event=event, subtask_id=subtask_id, agent=agent, detail=detail)
        )
        if self._tracer is not None:
            self._tracer.blackboard(event=event, subtask_id=subtask_id, agent=agent)

    @property
    def log(self) -> tuple[BlackboardEntry, ...]:
        return tuple(self._log)

    @property
    def records(self) -> tuple[SubtaskRecord, ...]:
        """Every record, in plan order."""
        return tuple(self._records.values())

    def completed(self) -> tuple[SubtaskRecord, ...]:
        return tuple(record for record in self._records.values() if record.completed)

    def unfinished(self) -> tuple[SubtaskRecord, ...]:
        """Subtasks that failed, timed out, or never reported.  The missing perspectives."""
        return tuple(record for record in self._records.values() if not record.completed)


class SubtaskHandle:
    """One worker's entire view of the blackboard: its own assignment, its own outcome."""

    def __init__(self, board: Blackboard, subtask_id: str) -> None:
        self._board = board
        self._subtask_id = subtask_id

    @property
    def subtask(self) -> Subtask:
        """This worker's own assignment.  There is no accessor for anyone else's."""
        return self._board._records[self._subtask_id].subtask

    @property
    def agent(self) -> str:
        return self.subtask.agent

    async def started(self) -> None:
        await self._board._transition(self._subtask_id, "started")

    async def completed(self, result: AgentResult) -> None:
        await self._board._transition(self._subtask_id, "completed", result=result)

    async def failed(self, error: str) -> None:
        await self._board._transition(self._subtask_id, "failed", error=error)

    async def timed_out(self, detail: str) -> None:
        await self._board._transition(self._subtask_id, "timeout", error=detail)
