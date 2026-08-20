"""Skill: ``find_guideline`` -- what a major body recommends on a topic.

Retrieval is filtered to the ``guideline`` category.  The optional ``aspect`` argument -- screening,
targets, first-line therapy, monitoring -- is folded into the query rather than filtered on, for the
same reason as ``recommend_lifestyle``'s domain: the corpus encodes the aspect in the ``doc_id``
(``guideline-<topic>-<aspect>``) and the title, so it is lexically present already, and a second
filter dimension would not be recorded in the ``retrieval`` trace event.

The result flags which retrieved notes carry a "Where guidance differs" section.  That flag is a
pointer, not the content: ``deep_research`` is the skill that reads those sections out, and
duplicating the extraction here would give two skills two chances to disagree about what the corpus
says.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from consilium.retrieval.corpus import DIFFERS_HEADING
from consilium.skills.base import (
    Passage,
    SkillContext,
    SkillResult,
    doc_ids,
    document_body,
    passages,
    require_retriever,
    skill,
)

NAME = "find_guideline"


class FindGuidelineArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        min_length=1, description="The condition or clinical topic, e.g. 'hypertension'."
    )
    aspect: str = Field(
        default="",
        description=(
            "Optionally narrow to one aspect, e.g. 'screening', 'blood pressure targets', "
            "'first-line treatment', 'monitoring interval'."
        ),
    )


class GuidelineHit(BaseModel):
    """One guideline note, summarized for citation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    title: str
    source: str
    #: True when the full note carries a "Where guidance differs" section, meaning authorities
    #: genuinely diverge on this topic and a single recommendation would misrepresent it.
    has_disagreement_section: bool


class FindGuidelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str
    aspect: str
    guidelines: list[GuidelineHit]
    passages: list[Passage]


@skill(
    name=NAME,
    description=(
        "Find what clinical guidance says about a topic: screening thresholds, treatment targets, "
        "first-line options, follow-up intervals, and where authorities disagree."
    ),
    category="research",
)
def find_guideline(args: FindGuidelineArgs, ctx: SkillContext) -> SkillResult:
    retriever = require_retriever(ctx, NAME)
    query = f"{args.topic} {args.aspect}".strip()
    hits = passages(retriever.search(query, skill=NAME, category="guideline", tracer=ctx.tracer))

    guidelines = [
        GuidelineHit(
            doc_id=hit.doc_id,
            title=hit.title,
            source=hit.source,
            has_disagreement_section=DIFFERS_HEADING
            in document_body(ctx, hit.doc_id, fallback=hit.text),
        )
        for hit in hits
    ]
    return SkillResult.success(
        NAME,
        FindGuidelineResult(
            topic=args.topic, aspect=args.aspect, guidelines=guidelines, passages=hits
        ),
        sources=doc_ids(hits),
    )
