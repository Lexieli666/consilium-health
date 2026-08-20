"""Skill: ``deep_research`` -- multi-query decomposition, per-claim attribution, explicit conflicts.

Three decisions in this file are worth defending, and all three are in docs/DESIGN.md.

**Corpus-only, no web search.**  This is a scoping decision, not an omission.  Web retrieval would
add a network dependency to a project whose test rule is that ``pytest -m "not network"`` passes
with no network, a provider choice nobody asked for, and a source-quality problem a portfolio
project cannot defend -- a page that ranks well is not a clinical authority.  The seam for pointing
the system at a larger real corpus is ``scripts/ingest_medquad.py``, which loads a public-domain
dataset offline.

**The sub-queries come from the model, not from a template and not from a second LLM call.**  The
agent writes them as tool arguments in the call it was already making, so the decomposition is free.
An LLM call issued from inside a skill was the alternative, and it fails on the trace schema:
``llm_call.caller`` is pattern-validated to ``planner``, ``synthesizer``, ``forced_answer`` or
``agent:<name>``, with no slot for a skill.  Adding one would change a frozen schema to buy a
capability the agent already has.  When the model supplies nothing, the fallback decomposition is
deterministic and stated below.

**Sub-queries are retrieved sequentially, in the order given.**  Threading them would be the obvious
reading of "parallel retrieval", and it is wrong here: retrieval is in-process CPU work over a few
hundred chunks, so there is no latency to win, and docs/EVALUATION.md defines a config-independent
recall@5 over the turn's *first* ``retrieval`` event.  Racing the sub-queries would make "first"
nondeterministic and that metric unreproducible.

**The disagreement section is read from the corpus, not judged.**  Guideline notes carry a
"Where guidance differs" section wherever authorities genuinely diverge; the skill extracts those
sections verbatim from the notes it retrieved.  Asking a model to decide whether two passages
disagree would be an unvalidated judge inside a tool, reported as a fact.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from consilium.retrieval.corpus import DIFFERS_HEADING
from consilium.skills.base import (
    Passage,
    SkillContext,
    SkillResult,
    document_body,
    passages,
    require_retriever,
    skill,
)

NAME = "deep_research"

#: The brief's range for the decomposition.  Enforced by truncating rather than by rejecting: a
#: model that proposes seven sub-queries has still done something useful, and failing the call
#: would spend one of its two tool calls on an argument-format complaint.
MAX_SUB_QUERIES = 5

#: Fallback decomposition when the model supplies no sub-queries.  Fixed aspect terms appended to
#: the question, chosen to span the corpus's own axes -- what the condition is, what guidance
#: recommends, how it is monitored -- rather than to sound like research.  Deterministic, so the
#: fallback path is as reproducible as the model-driven one.
FALLBACK_ASPECTS: tuple[str, ...] = (
    "diagnosis and criteria",
    "recommended first-line management",
    "monitoring and follow-up",
)


class DeepResearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, description="The research question, in full.")
    sub_queries: list[str] = Field(
        default_factory=list,
        description=(
            "Three to five sub-questions decomposing the question into separately retrievable "
            "parts. Leave empty to use a default decomposition."
        ),
    )


class Finding(BaseModel):
    """What one sub-query retrieved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub_query: str
    passages: list[Passage]


class Disagreement(BaseModel):
    """A "Where guidance differs" section, quoted from the note that carries it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    title: str
    text: str


class DeepResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    sub_queries: list[str]
    findings: list[Finding]
    #: Empty when the retrieved notes record no divergence.  An empty list means "the corpus does
    #: not say these authorities disagree", which is not the same as "they agree"; the answer must
    #: not upgrade one into the other.
    disagreements: list[Disagreement]


@skill(
    name=NAME,
    description=(
        "Research a question across the corpus by decomposing it into sub-questions, "
        "retrieving for each, and returning the evidence with its sources plus any place the "
        "corpus records that guidance differs. Use for open-ended evidence questions."
    ),
    category="research",
)
def deep_research(args: DeepResearchArgs, ctx: SkillContext) -> SkillResult:
    retriever = require_retriever(ctx, NAME)
    queries = plan_sub_queries(args.question, args.sub_queries)

    findings: list[Finding] = []
    sources: dict[str, None] = {}
    for query in queries:
        hits = passages(retriever.search(query, skill=NAME, tracer=ctx.tracer))
        findings.append(Finding(sub_query=query, passages=hits))
        for hit in hits:
            sources.setdefault(hit.doc_id, None)

    payload = DeepResearchResult(
        question=args.question,
        sub_queries=queries,
        findings=findings,
        disagreements=collect_disagreements(ctx, findings),
    )
    return SkillResult.success(NAME, payload, sources=tuple(sources))


def plan_sub_queries(question: str, proposed: Sequence[str]) -> list[str]:
    """The sub-queries to retrieve for: the model's, deduplicated and capped, or the fallback.

    The question itself always leads.  A decomposition that never asks the original question can
    miss the note that answers it directly, and that failure is invisible in the output -- every
    sub-query returns something, so the result looks complete.
    """
    queries: list[str] = [question.strip()]
    candidates = [item.strip() for item in proposed if item.strip()] or [
        f"{question.strip()} {aspect}" for aspect in FALLBACK_ASPECTS
    ]
    for candidate in candidates:
        if candidate not in queries:
            queries.append(candidate)
    return queries[:MAX_SUB_QUERIES]


def collect_disagreements(ctx: SkillContext, findings: Sequence[Finding]) -> list[Disagreement]:
    """Extract the "Where guidance differs" section of every note the findings retrieved."""
    seen: set[str] = set()
    found: list[Disagreement] = []
    for finding in findings:
        for hit in finding.passages:
            if hit.doc_id in seen:
                continue
            seen.add(hit.doc_id)
            section = _differs_section(document_body(ctx, hit.doc_id, fallback=hit.text))
            if section:
                found.append(Disagreement(doc_id=hit.doc_id, title=hit.title, text=section))
    return found


def _differs_section(body: str) -> str:
    """The text under the "Where guidance differs" heading, up to the next heading of any level."""
    start = body.find(DIFFERS_HEADING)
    if start < 0:
        return ""
    rest = body[start + len(DIFFERS_HEADING) :]
    end = rest.find("\n#")
    return rest[: end if end >= 0 else len(rest)].strip()
