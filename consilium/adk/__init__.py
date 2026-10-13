"""ADK layer: the planner-worker-synthesizer architecture as Google ADK agents on Vertex AI.

Selected with ``ORCHESTRATOR=adk``; ``legacy`` (the default) is ``consilium/router/``.  Imported
lazily by ``consilium/runtime.py`` so that the legacy path never imports ``google.adk``.
"""

from consilium.adk.orchestrator import AdkOrchestrator, ConsiliumRootAgent

__all__ = ["AdkOrchestrator", "ConsiliumRootAgent"]
