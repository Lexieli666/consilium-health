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

from consilium.agents import AGENT_TYPES, BaseAgent
from consilium.agents.loop import ReActLoop
from consilium.config import RunConfig, Settings
from consilium.llm.base import LLMProvider, Message
from consilium.llm.factory import make_provider
from consilium.memory.episodic import EpisodicMemory, SqliteEpisodicStore
from consilium.memory.store import InMemoryStore, MemoryStore
from consilium.memory.working import WorkingMemory
from consilium.retrieval.corpus import Document, load_corpus
from consilium.retrieval.hybrid import HybridRetriever
from consilium.retrieval.index import (
    EmbedderName,
    StoreName,
    make_embedder,
    make_store,
    open_retriever,
)
from consilium.router.planner import Planner
from consilium.router.router import DEFAULT_DEADLINE_SECONDS, Router
from consilium.router.synthesizer import Synthesizer
from consilium.safety.policy import Policy
from consilium.safety.red_flags import RedFlagTable
from consilium.safety.repair import OutputRepair, RepairResult
from consilium.safety.validator import PolicyValidator
from consilium.skills.base import SkillContext, SkillResult
from consilium.skills.registry import SkillRegistry
from consilium.skills.symptom_map import SymptomSystemMap
from consilium.trace import RiskLevel, RouteMode, ToolTransport, Tracer, TurnEvent, stopwatch


@dataclass(frozen=True)
class Runtime:
    """Everything a turn needs that outlives the turn."""

    settings: Settings
    config: RunConfig
    provider: LLMProvider
    registry: SkillRegistry
    policy: Policy
    red_flags: RedFlagTable
    validator: PolicyValidator
    repair: OutputRepair
    symptoms: SymptomSystemMap
    documents: Mapping[str, Document]
    #: Working memory, keyed by ``session_id``.  Held here because it outlives a turn; the
    #: ``WorkingMemory`` for one session is fetched per turn and injected, never shared globally.
    memory: MemoryStore
    #: ``None`` when ``RunConfig.retrieval`` is off -- the ``baseline_llm`` ablation row.  Skills
    #: that need it then fail with a stated reason rather than the run being impossible to express.
    retriever: HybridRetriever | None = None
    #: ``None`` unless a store was configured.  Cross-session recall is off in every measured run;
    #: see ``consilium/memory/episodic.py`` for why, and docs/EVALUATION.md for the consequence.
    episodic: EpisodicMemory | None = None

    def context(
        self,
        *,
        agent: str,
        tracer: Tracer | None = None,
        transport: ToolTransport = "internal",
    ) -> SkillContext:
        """A per-turn skill context labelled for one agent.

        ``transport`` defaults to ``internal`` because that is what a turn is.  ``consilium
        mcp_server`` passes ``mcp``, which is the only thing separating an MCP host's tool call
        from the loop's in the trace -- the skill, the registry and the substrate underneath are
        the same objects.
        """
        return SkillContext(
            retriever=self.retriever,
            red_flags=self.red_flags,
            symptoms=self.symptoms,
            documents=self.documents,
            tracer=tracer,
            agent=agent,
            transport=transport,
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
            validator=self.validator,
        )
        return agent_type(
            provider=self.provider, registry=self.registry, policy=self.policy, loop=loop
        )

    def router(
        self,
        *,
        tracer: Tracer | None = None,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> Router:
        """Build the router for one turn.

        Per turn rather than per process, because the context factory closes over the turn's tracer.
        Everything expensive -- the corpus, the index, the tables -- is already built and is shared
        by reference; what is constructed here is three small objects and two closures.
        """
        return Router(
            planner=Planner(provider=self.provider, policy=self.policy),
            synthesizer=Synthesizer(provider=self.provider),
            agent_factory=self.agent,
            context_factory=lambda name: self.context(agent=name, tracer=tracer),
            config=self.config,
            deadline_seconds=deadline_seconds,
        )


def build_runtime(
    settings: Settings,
    *,
    config: RunConfig | None = None,
    provider: LLMProvider | None = None,
    script: Path | None = None,
    embedder: EmbedderName = "bge",
    store: StoreName = "chroma",
    memory: MemoryStore | None = None,
    episodic: bool = False,
    episodic_recall: bool = False,
) -> Runtime:
    """Assemble a :class:`Runtime` from settings.

    ``provider`` is injectable so that a test, or the offline demo, can supply a ``MockProvider``
    without going through the environment.  ``embedder`` and ``store`` default to the real pipeline
    -- the one that produces the retrieval numbers -- because a default that quietly used the
    offline seams would make the demo and the measurement two different systems.
    """
    run_config = config or RunConfig(name="full")
    documents = load_corpus(settings.corpus_dir)
    built_embedder = make_embedder(embedder)

    retriever: HybridRetriever | None = None
    if run_config.retrieval:
        retriever, _ = open_retriever(
            corpus_dir=settings.corpus_dir,
            embedder=built_embedder,
            store=make_store(store, path=settings.chroma_dir),
        )

    episodic_memory: EpisodicMemory | None = None
    if episodic:
        # The same embedder as retrieval, deliberately: two embedding models would mean two
        # downloads and two dimensions to keep in step, for one lookup that runs once per turn.
        episodic_memory = EpisodicMemory(
            SqliteEpisodicStore(settings.episodic_db_path),
            built_embedder,
            recall_enabled=episodic_recall,
        )

    policy = Policy.from_yaml(settings.policy_path)
    # The red-flag table is loaded from the path the policy names, not from a second setting: the
    # policy references the file rather than restating it, and resolving the reference here is what
    # makes that reference load-bearing instead of documentation.
    red_flags = RedFlagTable.from_yaml(policy.red_flags_path or settings.red_flags_path)

    return Runtime(
        settings=settings,
        config=run_config,
        provider=provider or make_provider(settings, script=script),
        registry=SkillRegistry.discover(),
        policy=policy,
        red_flags=red_flags,
        validator=PolicyValidator(policy),
        repair=OutputRepair(policy),
        symptoms=SymptomSystemMap.from_yaml(settings.symptom_systems_path),
        documents={document.doc_id: document for document in documents},
        memory=memory or InMemoryStore(),
        retriever=retriever,
        episodic=episodic_memory,
    )


@dataclass(frozen=True)
class TurnOutcome:
    """What one user turn produced, and the trace record that proves it."""

    answer: str
    sources: tuple[str, ...]
    risk_level: RiskLevel
    mode: RouteMode
    agents: tuple[str, ...]
    #: True when the planner could not produce a usable plan and the single-agent default fired.
    fallback: bool
    #: What the safety layer found and did.  Carried on the outcome so an interface can report it
    #: without re-reading the trace.
    safety: RepairResult
    #: Specialists whose subtask failed or timed out; named in the delivered answer.
    missing: tuple[str, ...]
    trace_id: str
    wall_ms: float
    tool_results: tuple[SkillResult, ...]
    turn_event: TurnEvent


async def run_turn(
    runtime: Runtime,
    question: str,
    *,
    tracer: Tracer,
    agent: str | None = None,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> TurnOutcome:
    """Route one question, deliver an answer, and write the ``turn`` event.

    The routing decision belongs to ``consilium/router/``.  What stays here is the boundary: the
    red-flag assessment of the *input*, the wall clock, and the single ``turn`` event written last.

    ``agent`` pins one specialist and skips routing.  It is the CLI's ``--agent`` flag, a debugging
    affordance; no measured run uses it.

    **Memory is per session and injected for the duration of the turn.**  The ``WorkingMemory`` is
    fetched from the store by ``tracer.session_id``, its compacted history is handed to every worker
    of the turn -- the same object, which is how sharing between agents within one turn is achieved
    -- and the exchange is recorded afterwards.  ``RunConfig.memory=False`` (the ``full_no_memory``
    ablation) skips all of it: no history in, nothing recorded out.

    **The risk level comes from the red-flag table applied to the question, never from the answer.**
    That is what makes it immovable by anything the model or the synthesizer does.

    **The safety layer runs between routing and delivery.**  ``PolicyValidator`` checks the routed
    answer and records every violation; ``OutputRepair`` fixes what it found and records every
    repair.  The two are separate counts and are reported as two rates.  The ``turn`` event's three
    escalation fields are then filled from the repair result: pre-repair describes the model's own
    answer, post-repair describes the delivered one -- **post-repair is red-flag recall** -- and
    ``repair_applied`` says which of the two saved it.
    """
    assessment = runtime.red_flags.assess(question)
    router = runtime.router(tracer=tracer, deadline_seconds=deadline_seconds)

    working = runtime.memory.get(tracer.session_id) if runtime.config.memory else None
    history = _history(runtime, working, question)

    with stopwatch() as elapsed_ms:
        routed = await router.handle(question, tracer=tracer, history=history, pinned_agent=agent)
    wall_ms = elapsed_ms()

    guarded_by = routed.agents[0] if len(routed.agents) == 1 else None
    violations = runtime.validator.check_output(
        routed.answer, assessment=assessment, agent=guarded_by, tracer=tracer
    )
    repaired = runtime.repair.apply(routed.answer, violations, agent=guarded_by, tracer=tracer)

    if working is not None:
        # The *delivered* answer, not the model's raw one.  A later turn's context must match what
        # the user actually saw, or the conversation the model believes it had is not the one that
        # happened -- and a redacted sentence would come back through memory.
        working.record(
            question=question,
            answer=repaired.answer,
            tool_results=[
                result for agent_result in routed.results for result in agent_result.tool_results
            ],
            risk_level=assessment.urgency,
        )
        runtime.memory.save(working)
        if runtime.episodic is not None:
            runtime.episodic.remember(
                session_id=tracer.session_id,
                question=working.exchanges[0].question,
                key_findings=repaired.answer,
                risk_level=assessment.urgency,
                sources=routed.sources,
            )

    event = tracer.turn(
        question=question,
        answer=repaired.answer,
        risk_level=assessment.urgency,
        wall_ms=wall_ms,
        red_flag_matched=assessment.matched,
        red_flag_matched_raw=assessment.matched_raw,
        red_flag_negation_suppressed=assessment.negation_suppressed,
        escalation_present_pre_repair=repaired.escalation_present_pre_repair,
        escalation_present_post_repair=repaired.escalation_present_post_repair,
        repair_applied=repaired.repair_applied,
    )
    return TurnOutcome(
        answer=repaired.answer,
        sources=routed.sources,
        risk_level=assessment.urgency,
        mode=routed.mode,
        agents=routed.agents,
        fallback=routed.fallback,
        missing=routed.missing,
        safety=repaired,
        trace_id=tracer.trace_id,
        wall_ms=wall_ms,
        tool_results=tuple(
            result for agent_result in routed.results for result in agent_result.tool_results
        ),
        turn_event=event,
    )


def _history(runtime: Runtime, working: WorkingMemory | None, question: str) -> list[Message]:
    """The prior-turn context handed to every worker of this turn.

    Episodic recall is prepended only when a store is configured *and* recall is enabled, which no
    measured run does.  Its absence in the ablation is a deliberate measurement decision, stated in
    ``consilium/memory/episodic.py`` and in docs/EVALUATION.md, not an oversight.
    """
    if working is None:
        return []
    history = working.history()
    if runtime.episodic is None:
        return history

    recalled = runtime.episodic.recall(question)
    if not recalled:
        return history
    lines = [f"- {item.episode.question} -> {item.episode.key_findings[:200]}" for item in recalled]
    note = "[recalled from earlier sessions]\n" + "\n".join(lines)
    return [Message(role="user", content=note), *history]
