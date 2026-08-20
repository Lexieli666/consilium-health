"""Skill: ``recommend_lifestyle`` -- diet, activity, sleep and adherence guidance for a condition.

Retrieval is filtered to the ``lifestyle`` category, and the optional ``domain`` argument is folded
into the query text rather than used as a second filter.  The corpus encodes the domain in the
``doc_id`` (``lifestyle-<topic>-<domain>``) and states it in the title, so it is already lexically
present; adding a metadata filter for it would mean a second filter dimension the retrieval trace
does not record, and ``retrieval.category_filter`` is a single field by design.

Output is descriptive, matching the corpus: these are the options guidance describes, not
instructions to follow.  The corpus contains no doses, so neither can this.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from consilium.skills.base import (
    Passage,
    SkillContext,
    SkillResult,
    doc_ids,
    passages,
    require_retriever,
    skill,
)

NAME = "recommend_lifestyle"

LifestyleDomain = Literal["diet", "activity", "sleep", "adherence"]


class RecommendLifestyleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(
        min_length=1,
        description="The condition to find lifestyle guidance for, e.g. 'hypertension'.",
    )
    domain: LifestyleDomain | None = Field(
        default=None,
        description="Optionally focus on one domain: diet, activity, sleep or adherence.",
    )


class RecommendLifestyleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    condition: str
    domain: LifestyleDomain | None
    passages: list[Passage]


@skill(
    name=NAME,
    description=(
        "Retrieve diet, physical-activity, sleep or treatment-adherence guidance for a named "
        "condition. Returns descriptive options from the corpus, never dosing or instructions."
    ),
    category="guidance",
)
def recommend_lifestyle(args: RecommendLifestyleArgs, ctx: SkillContext) -> SkillResult:
    retriever = require_retriever(ctx, NAME)
    query = f"{args.condition} {args.domain}" if args.domain else args.condition
    hits = passages(retriever.search(query, skill=NAME, category="lifestyle", tracer=ctx.tracer))
    return SkillResult.success(
        NAME,
        RecommendLifestyleResult(condition=args.condition, domain=args.domain, passages=hits),
        sources=doc_ids(hits),
    )
