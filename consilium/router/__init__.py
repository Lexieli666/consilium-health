"""Router layer: planner-worker orchestration with a blackboard.

``planner`` decides which specialists answer, ``blackboard`` holds the assignments and the results,
``router`` dispatches and enforces the turn deadline, ``synthesizer`` merges what came back under
fixed precedence.

It is **not** a swarm, and nothing in this package or its docs calls it one.  There is a central
planner, an explicit assignment of subtasks to named workers, and a shared board -- which is
planner-worker orchestration, and which the reference implementation's own description also
calls centralized decomposition.  The dedup-and-summarize step in ``consilium/memory/`` is
**context compaction**, never "entropy management": nothing there computes an entropy.
"""

from consilium.router.blackboard import (
    Blackboard,
    BlackboardEntry,
    SubtaskHandle,
    SubtaskRecord,
)
from consilium.router.plan import Plan, PlanItem, Subtask, number
from consilium.router.planner import (
    FALLBACK_AGENT,
    FALLBACK_OBJECTIVE,
    Planner,
    extract_json_object,
)
from consilium.router.router import (
    DEFAULT_DEADLINE_SECONDS,
    AgentFactory,
    ContextFactory,
    Router,
    RouteResult,
)
from consilium.router.synthesizer import (
    AGENT_ORDER,
    ALL_FAILED_ANSWER,
    PRECEDENCE,
    Synthesizer,
    order_by_precedence,
)

__all__ = [
    "AGENT_ORDER",
    "ALL_FAILED_ANSWER",
    "DEFAULT_DEADLINE_SECONDS",
    "FALLBACK_AGENT",
    "FALLBACK_OBJECTIVE",
    "PRECEDENCE",
    "AgentFactory",
    "Blackboard",
    "BlackboardEntry",
    "ContextFactory",
    "Plan",
    "PlanItem",
    "Planner",
    "RouteResult",
    "Router",
    "Subtask",
    "SubtaskHandle",
    "SubtaskRecord",
    "Synthesizer",
    "Worker",
    "extract_json_object",
    "number",
    "order_by_precedence",
]
