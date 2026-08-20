"""Agents layer: three specialists sharing one ReAct loop.

``ConsultationAgent``, ``DiagnosticAgent`` and ``ResearchAgent`` differ in exactly two things:
their system prompt (``consilium/agents/prompts.py``) and their permitted-skill list
(``data/policy.yaml``).  All three are registered with all seven skills and narrowed by the policy
at construction; there is no hard-coded tool wiring anywhere in this package.

``loop.py`` belongs here rather than at ``consilium/loop.py``.  The brief writes the shorter path,
but the same brief requires that a reviewer be able to point at any file and name its layer, and a
``loop.py`` sitting beside ``cli.py`` and ``config.py`` reads as substrate.  It is the engine the
agents share, so it lives with them.  Recorded in CLAUDE.md section 10.
"""

from consilium.agents.base import AgentResult, BaseAgent
from consilium.agents.consultation import ConsultationAgent
from consilium.agents.diagnostic import DiagnosticAgent
from consilium.agents.loop import (
    BUDGET_EXHAUSTED_OBSERVATION,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_TOOL_CALLS,
    EMPTY_ANSWER_FALLBACK,
    LoopResult,
    ReActLoop,
)
from consilium.agents.prompts import CONSULTATION, DIAGNOSTIC, RESEARCH, SHARED_RULES
from consilium.agents.research import ResearchAgent

#: The three specialist classes, keyed by the name the planner and the policy use.  One mapping, so
#: the router never has to build a name -> class dispatch of its own.
AGENT_TYPES: dict[str, type[BaseAgent]] = {
    ConsultationAgent.name: ConsultationAgent,
    DiagnosticAgent.name: DiagnosticAgent,
    ResearchAgent.name: ResearchAgent,
}

#: The fallback specialist, used when the planner cannot produce a plan.
DEFAULT_AGENT = ConsultationAgent.name

__all__ = [
    "AGENT_TYPES",
    "BUDGET_EXHAUSTED_OBSERVATION",
    "CONSULTATION",
    "DEFAULT_AGENT",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_TOOL_CALLS",
    "DIAGNOSTIC",
    "EMPTY_ANSWER_FALLBACK",
    "RESEARCH",
    "SHARED_RULES",
    "AgentResult",
    "BaseAgent",
    "ConsultationAgent",
    "DiagnosticAgent",
    "LoopResult",
    "ReActLoop",
    "ResearchAgent",
]
