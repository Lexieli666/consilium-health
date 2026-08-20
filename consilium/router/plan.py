"""Router layer: the plan -- what the planner produces and how a subtask is identified.

Kept in its own module because three components read it (the planner writes it, the blackboard
tracks it, the synthesizer labels sections with it) and none of them should have to import another.

``Subtask`` is the router's own model.  ``consilium.trace.PlannedSubtask`` is the substrate's, and
the mapping between them is one method here.  Substrate may not depend on a layer above it, so the
trace declares its own shape; having the router map onto it -- rather than the trace importing this
-- is what keeps that direction true.

**Subtask ids are deterministic**, ``<index>-<agent>``.  A uuid per subtask would make two traces of
the same plan textually different for no reason, and comparing traces across ablation presets is a
thing this project actually does.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from consilium.trace import PlannedSubtask


class PlanItem(BaseModel):
    """One entry of the planner's JSON output, before an id is assigned.

    ``extra="forbid"`` so that a planner returning an invented field is a parse failure and a
    counted fallback, rather than a plan silently missing whatever that field was meant to say.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str
    objective: str = Field(min_length=1)
    why: str = Field(default="", description="Why this agent, in the planner's own words.")


class Plan(BaseModel):
    """The planner's whole reply."""

    model_config = ConfigDict(extra="forbid")

    subtasks: list[PlanItem] = Field(min_length=1)


@dataclass(frozen=True)
class Subtask:
    """One assignment, with the id the blackboard and the trace both use."""

    subtask_id: str
    agent: str
    objective: str
    why: str

    def to_trace(self) -> PlannedSubtask:
        return PlannedSubtask(
            subtask_id=self.subtask_id, agent=self.agent, objective=self.objective, why=self.why
        )


def number(items: list[PlanItem]) -> list[Subtask]:
    """Assign ids in plan order."""
    return [
        Subtask(
            subtask_id=f"{index}-{item.agent}",
            agent=item.agent,
            objective=item.objective.strip(),
            why=item.why.strip(),
        )
        for index, item in enumerate(items, start=1)
    ]
