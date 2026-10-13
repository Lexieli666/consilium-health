"""Graph layer: the planner -> retrieve -> synthesizer -> validator turn as a LangGraph graph.

Selected with ``ORCHESTRATOR=langgraph``; ``legacy`` (the default) is ``consilium/router/`` and
``adk`` is ``consilium/adk/``.  Imported lazily by ``consilium/runtime.py`` so that the other two
paths never import ``langgraph``.  ``python -m consilium.graph "<question>"`` runs one turn.
"""

from consilium.graph.orchestrator import (
    ConsiliumGraph,
    GraphState,
    LangGraphOrchestrator,
    close_all,
    graph_for,
)
from consilium.graph.tracing import configure_langsmith

__all__ = [
    "ConsiliumGraph",
    "GraphState",
    "LangGraphOrchestrator",
    "close_all",
    "configure_langsmith",
    "graph_for",
]
