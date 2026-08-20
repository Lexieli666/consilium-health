"""Skills layer: the envelope, the context, and the decorator that declares a skill.

A skill is a plain function of two arguments -- a validated Pydantic argument model and a
:class:`SkillContext` carrying the substrate it is allowed to reach -- returning a
:class:`SkillResult`.  Nothing here knows about agents, the ReAct loop, or the router; a skill is
the smallest unit of work the system can attribute a source document to.

**Skills are synchronous.**  Every one of them is local CPU work: BM25 scoring over a few hundred
chunks, a numpy matmul, a regex pass over a rule table.  Writing them ``async def`` would be async
syntax over code that never awaits, and it would not make ``sentence-transformers`` release the
event loop anyway.  The registry is what bridges to the async world, by dispatching the call
through a worker thread -- see :meth:`consilium.skills.registry.SkillRegistry.execute`.

**A skill never raises into the loop.**  Every failure -- a validation error on the model's tool
arguments, a missing retriever, a bug inside the skill -- comes back as ``ok=False`` with an
``error`` string, and is recorded as a ``tool_call`` event with ``ok=False``.  A skill that raised
would abort the agent's turn, and an agent that dies on a malformed tool call is an agent whose
failure mode is invisible to every metric in docs/EVALUATION.md.  The raising version also fails in
the worst place: mid-turn, after the tokens for the tool call have already been paid for.

**Every retrieval-backed skill returns the ``doc_id`` values it used**, in ``sources``.  The
registry copies them onto the ``tool_call`` event's ``source_doc_ids``, which is what makes an
answer traceable to documents and what recall@5 is computed against.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, get_type_hints

from pydantic import BaseModel, ConfigDict, Field

from consilium.retrieval.types import ScoredChunk

if TYPE_CHECKING:  # pragma: no cover - imports for typing only, and one of them is a layer peer
    from consilium.retrieval.corpus import Document
    from consilium.retrieval.hybrid import HybridRetriever
    from consilium.safety.red_flags import RedFlagTable
    from consilium.skills.symptom_map import SymptomSystemMap
    from consilium.trace import Tracer

#: What kind of work a skill does.  Deliberately *not* the corpus ``Category`` literal from
#: ``consilium.retrieval.types``, which is a property of a document rather than of a tool; the two
#: vocabularies overlap on the word "coding" and nowhere else, and merging them would tie the
#: skill taxonomy to the corpus layout.
SkillCategory = Literal["knowledge", "triage", "guidance", "coding", "research"]


class SkillError(RuntimeError):
    """Raised only at declaration time -- a malformed ``@skill`` -- never during execution."""


class Passage(BaseModel):
    """One retrieved chunk, in the shape a skill hands back to the model.

    Carries the full chunk text rather than a snippet.  Truncating evidence on the way to the model
    would raise faithfulness failures that look like model errors and are actually harness errors.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    chunk_index: int = Field(ge=0)
    title: str
    category: str
    source: str
    text: str
    score: float


class SkillResult(BaseModel):
    """The envelope every skill returns, successful or not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: float = Field(default=0.0, ge=0)
    sources: tuple[str, ...] = ()

    @classmethod
    def success(cls, skill: str, payload: BaseModel, *, sources: Sequence[str] = ()) -> SkillResult:
        """Build a successful result from a typed payload model.

        Skills return typed payloads and this is the single place they become the untyped ``data``
        mapping the trace stores.  The alternative -- each skill assembling its own dict -- makes
        the observation the model sees unverifiable, because there is no schema to check it against.
        """
        return cls(
            skill=skill, ok=True, data=payload.model_dump(mode="json"), sources=tuple(sources)
        )

    @classmethod
    def failure(cls, skill: str, error: str) -> SkillResult:
        return cls(skill=skill, ok=False, error=error)

    def with_latency(self, latency_ms: float) -> SkillResult:
        """Return a copy stamped with its measured duration.

        Timing is the registry's job, not the skill's: a skill that timed itself would exclude
        argument validation and the dispatch hop, and the ``tool_call`` event is supposed to say how
        long the *tool call* took.
        """
        return self.model_copy(update={"latency_ms": latency_ms})

    def to_observation(self) -> str:
        """Render the result as the ``tool`` message the ReAct loop feeds back to the model."""
        if not self.ok:
            return f"ERROR: {self.error}"
        return self.model_dump_json(include={"data", "sources"})


@dataclass(frozen=True)
class SkillContext:
    """Everything a skill is allowed to reach, injected per turn.

    Assembled once per turn and handed to every skill invocation of that turn, so that a skill has
    no module-level state of its own and two concurrent sessions cannot share anything through it.

    ``retriever`` is optional because ``RunConfig.retrieval`` can be off: the ``baseline_llm``
    ablation row is defined by the absence of retrieval, and a preset that has to be simulated by
    editing code is not an ablation.  A retrieval-backed skill invoked without a retriever returns
    ``ok=False`` with a plain explanation, which is a measurable outcome rather than a crash.
    """

    retriever: HybridRetriever | None = None
    red_flags: RedFlagTable | None = None
    symptoms: SymptomSystemMap | None = None
    #: ``doc_id`` -> full document.  Needed only by ``deep_research``, which reads whole notes to
    #: find their "Where guidance differs" sections; retrieval returns chunks, and the section a
    #: disagreement lives in is usually not the chunk that matched the query.
    documents: Mapping[str, Document] = field(default_factory=dict)
    tracer: Tracer | None = None
    #: Which agent is making the call.  Recorded on the ``tool_call`` event; the per-agent tool-use
    #: breakdown in docs/EVALUATION.md is computed from it.
    agent: str = "unknown"


class SkillCallable(Protocol):
    """The shape of a skill implementation."""

    def __call__(self, args: Any, ctx: SkillContext) -> SkillResult: ...


FuncT = TypeVar("FuncT", bound=SkillCallable)


@dataclass(frozen=True)
class Skill:
    """A declared skill: its identity, its argument model, and its implementation."""

    name: str
    description: str
    category: SkillCategory
    args_model: type[BaseModel]
    func: SkillCallable
    requires_retrieval: bool

    def parameters_schema(self) -> dict[str, Any]:
        """The JSON Schema for this skill's arguments, derived from the Pydantic model.

        Derived, never hand-written.  A second copy of the schema in JSON would drift from the model
        the first time a field gained a default, and the drift would show up as the model passing
        arguments the validator rejects -- a failure that looks like a model problem and is not one.
        """
        schema = self.args_model.model_json_schema()
        # The model's class name is not useful to the LLM and costs tokens in every prompt that
        # offers the tool.  The field-level titles are left alone: they carry the field names.
        schema.pop("title", None)
        return schema


#: Skills declared by ``@skill`` at import time, in declaration order.  A module-level list is safe
#: here in a way a module-level *session* would not be: it is written once per process, at import,
#: and read-only afterwards.  ``SkillRegistry`` copies it rather than aliasing it, so a test can
#: build a registry over a subset without disturbing the declarations.
_DECLARED: list[Skill] = []


def skill(
    *,
    name: str,
    description: str,
    category: SkillCategory,
    requires_retrieval: bool = True,
) -> Callable[[FuncT], FuncT]:
    """Declare a function as a skill.

    The argument model is read off the function's own ``args`` annotation rather than passed in.
    Passing it would mean stating the model twice -- once in the signature that mypy checks and once
    in the decorator that the registry reads -- and only one of the two copies would be verified.
    """

    def decorator(func: FuncT) -> FuncT:
        hints = get_type_hints(func)
        args_model = hints.get("args")
        if args_model is None:
            raise SkillError(
                f"skill {name!r}: the implementation needs an annotated `args` parameter"
            )
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise SkillError(
                f"skill {name!r}: `args` must be annotated with a pydantic model; "
                f"got {args_model!r}"
            )
        if any(declared.name == name for declared in _DECLARED):
            raise SkillError(f"skill {name!r} is declared twice")

        _DECLARED.append(
            Skill(
                name=name,
                description=description,
                category=category,
                args_model=args_model,
                func=func,
                requires_retrieval=requires_retrieval,
            )
        )
        return func

    return decorator


def declared_skills() -> tuple[Skill, ...]:
    """Every skill declared so far, in declaration order."""
    return tuple(_DECLARED)


def require_retriever(ctx: SkillContext, name: str) -> HybridRetriever:
    """Narrow ``ctx.retriever`` to non-``None`` for a skill declared ``requires_retrieval=True``.

    The registry has already refused the call when retrieval is off, so this is unreachable in the
    ordinary path -- but the type is genuinely optional and a skill that reached for ``.search`` on
    ``None`` would raise ``AttributeError`` with no indication of which knob caused it.  Raising
    :class:`SkillError` here turns a wiring mistake into a sentence, and the registry converts it
    into ``ok=False`` like any other skill failure.
    """
    if ctx.retriever is None:
        raise SkillError(f"{name} was invoked without a retriever; retrieval is disabled")
    return ctx.retriever


def document_body(ctx: SkillContext, doc_id: str, *, fallback: str = "") -> str:
    """The full note body for ``doc_id``, or ``fallback`` when the corpus map was not injected.

    Retrieval returns chunks, and a skill that needs to know something about the *whole* note --
    whether it carries a "Where guidance differs" section, say -- cannot learn it from the chunk
    that happened to match.  ``SkillContext.documents`` carries the loaded notes for exactly that,
    and defaults to empty so a caller that has no corpus map still gets a degraded answer rather
    than an exception.
    """
    document = ctx.documents.get(doc_id)
    return document.body if document is not None else fallback


def passages(scored: Sequence[ScoredChunk]) -> list[Passage]:
    """Convert retrieval hits into the passage shape skills return."""
    return [
        Passage(
            doc_id=hit.chunk.doc_id,
            chunk_index=hit.chunk.chunk_index,
            title=hit.chunk.title,
            category=hit.chunk.category,
            source=hit.chunk.source,
            text=hit.chunk.text,
            score=hit.score,
        )
        for hit in scored
    ]


def doc_ids(items: Sequence[Passage]) -> tuple[str, ...]:
    """Deduplicated ``doc_id`` values in first-seen order, for ``SkillResult.sources``."""
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item.doc_id, None)
    return tuple(seen)
