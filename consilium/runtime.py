"""The composition root and the turn boundary.

Two responsibilities, both about wiring rather than domain logic, and both needed identically by
the CLI, the HTTP API and the eval harness:

**Composition.**  Somewhere has to know that a ``ConsultationAgent`` needs a provider, a registry, a
policy and a skill context, and that the context needs a retriever, two rule tables and the loaded
corpus.  The choice is whether that knowledge lives in one named place or is repeated in three entry
points -- and repeating it is how the three end up subtly differently configured, which surfaces
later as an evaluation number that cannot be reproduced through the API.  This module imports from
every layer, which is exactly why it is not itself one.

**The turn boundary.**  One user turn is one ``Tracer``, one trace file, and one ``turn`` event
written last.  :func:`run_turn` owns that invariant so no entry point has to remember it.  From
Phase 5 the routing decision inside a turn belongs to ``consilium/router/``; the boundary around it
stays here.

``Runtime`` is built once per process and is read-only.  Everything per turn -- the tracer, the
session's working memory, the ``SkillContext`` that carries them -- is created per turn and
injected; nothing session-scoped is stored here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from consilium.agents import AGENT_TYPES, DEFAULT_AGENT, BaseAgent
from consilium.agents.loop import ReActLoop
from consilium.config import RunConfig, Settings
from consilium.llm.base import LLMProvider
from consilium.llm.factory import make_provider
from consilium.retrieval.corpus import Document, load_corpus
from consilium.retrieval.hybrid import HybridRetriever
from consilium.retrieval.index import (
    EmbedderName,
    StoreName,
    make_embedder,
    make_store,
    open_retriever,
)
from consilium.safety.escalation import escalation_present
from consilium.safety.policy import Policy
from consilium.safety.red_flags import RedFlagTable
from consilium.skills.base import SkillContext, SkillResult
from consilium.skills.registry import SkillRegistry
from consilium.skills.symptom_map import SymptomSystemMap
from consilium.trace import RiskLevel, RouteMode, Tracer, TurnEvent, stopwatch


@dataclass(frozen=True)
class Runtime:
    """Everything a turn needs that outlives the turn."""

    settings: Settings
    config: RunConfig
    provider: LLMProvider
    registry: SkillRegistry
    policy: Policy
    red_flags: RedFlagTable
    symptoms: SymptomSystemMap
    documents: Mapping[str, Document]
    #: ``None`` when ``RunConfig.retrieval`` is off -- the ``baseline_llm`` ablation row.  Skills
    #: that need it then fail with a stated reason rather than the run being impossible to express.
    retriever: HybridRetriever | None = None

    def context(self, *, agent: str, tracer: Tracer | None = None) -> SkillContext:
        """A per-turn skill context labelled for one agent."""
        return SkillContext(
            retriever=self.retriever,
            red_flags=self.red_flags,
            symptoms=self.symptoms,
            documents=self.documents,
            tracer=tracer,
            agent=agent,
        )

    def agent(self, name: str) -> BaseAgent:
        """Construct one specialist, with the run configuration's budgets already applied."""
        try:
            agent_type = AGENT_TYPES[name]
        except KeyError:
            known = ", ".join(sorted(AGENT_TYPES))
            raise KeyError(f"unknown agent {name!r}; known agents: {known}") from None
        loop = ReActLoop(
            provider=self.provider,
            registry=self.registry,
            max_iterations=self.config.max_iterations,
            max_tool_calls=self.config.max_tool_calls,
        )
        return agent_type(
            provider=self.provider, registry=self.registry, policy=self.policy, loop=loop
        )


def build_runtime(
    settings: Settings,
    *,
    config: RunConfig | None = None,
    provider: LLMProvider | None = None,
    script: Path | None = None,
    embedder: EmbedderName = "bge",
    store: StoreName = "chroma",
) -> Runtime:
    """Assemble a :class:`Runtime` from settings.

    ``provider`` is injectable so that a test, or the offline demo, can supply a ``MockProvider``
    without going through the environment.  ``embedder`` and ``store`` default to the real pipeline
    -- the one that produces the retrieval numbers -- because a default that quietly used the
    offline seams would make the demo and the measurement two different systems.
    """
    run_config = config or RunConfig(name="full")
    documents = load_corpus(settings.corpus_dir)

    retriever: HybridRetriever | None = None
    if run_config.retrieval:
        retriever, _ = open_retriever(
            corpus_dir=settings.corpus_dir,
            embedder=make_embedder(embedder),
            store=make_store(store, path=settings.chroma_dir),
        )

    return Runtime(
        settings=settings,
        config=run_config,
        provider=provider or make_provider(settings, script=script),
        registry=SkillRegistry.discover(),
        policy=Policy.from_yaml(settings.policy_path),
        red_flags=RedFlagTable.from_yaml(settings.red_flags_path),
        symptoms=SymptomSystemMap.from_yaml(settings.symptom_systems_path),
        documents={document.doc_id: document for document in documents},
        retriever=retriever,
    )


@dataclass(frozen=True)
class TurnOutcome:
    """What one user turn produced, and the trace record that proves it."""

    answer: str
    sources: tuple[str, ...]
    risk_level: RiskLevel
    mode: RouteMode
    agents: tuple[str, ...]
    trace_id: str
    wall_ms: float
    tool_results: tuple[SkillResult, ...]
    turn_event: TurnEvent


async def run_turn(
    runtime: Runtime,
    question: str,
    *,
    tracer: Tracer,
    agent: str = DEFAULT_AGENT,
) -> TurnOutcome:
    """Run one turn through one specialist and write the ``turn`` event.

    Phase 4 has no router, so the specialist is named by the caller and the recorded mode is always
    ``single``.  Phase 5 replaces the dispatch in the middle of this function with the planner and
    the blackboard; the tracer, the red-flag assessment and the ``turn`` event stay here, because
    they are properties of the turn rather than of how it was routed.

    The three escalation fields describe **this** phase honestly.  There is no ``OutputRepair`` yet,
    so the delivered answer is the model's own answer: pre and post are the same value and
    ``repair_applied`` is False.  When the repair lands in Phase 7 the post-repair field starts
    describing a different string, which is precisely the distinction those fields exist to draw.
    """
    assessment = runtime.red_flags.assess(question)
    specialist = runtime.agent(agent)
    ctx = runtime.context(agent=specialist.name, tracer=tracer)

    with stopwatch() as elapsed_ms:
        result = await specialist.answer(question, ctx=ctx)
    wall_ms = elapsed_ms()

    escalated = escalation_present(result.answer)
    event = tracer.turn(
        question=question,
        answer=result.answer,
        risk_level=assessment.urgency,
        wall_ms=wall_ms,
        red_flag_matched=assessment.matched,
        red_flag_matched_raw=assessment.matched_raw,
        red_flag_negation_suppressed=assessment.negation_suppressed,
        escalation_present_pre_repair=escalated,
        escalation_present_post_repair=escalated,
        repair_applied=False,
    )
    return TurnOutcome(
        answer=result.answer,
        sources=result.sources,
        risk_level=assessment.urgency,
        mode="single",
        agents=(specialist.name,),
        trace_id=tracer.trace_id,
        wall_ms=wall_ms,
        tool_results=result.tool_results,
        turn_event=event,
    )
