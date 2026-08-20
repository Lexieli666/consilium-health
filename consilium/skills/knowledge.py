"""Skill: ``search_knowledge`` -- general retrieval over the whole corpus.

The one skill with no category filter.  Every other retrieval-backed skill narrows the pool to the
category it is about, which is what stops ``lookup_disease_code`` competing with lifestyle prose for
a top-5 slot; this one is the escape hatch for questions that do not fit a category, and for the
single-agent RAG ablation, where it is the only retrieval the configuration has.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from consilium.retrieval.types import Category
from consilium.skills.base import (
    Passage,
    SkillContext,
    SkillResult,
    doc_ids,
    passages,
    require_retriever,
    skill,
)

NAME = "search_knowledge"


class SearchKnowledgeArgs(BaseModel):
    """Arguments for ``search_knowledge``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, description="What to look up, in natural language.")
    category: Category | None = Field(
        default=None,
        description=(
            "Optionally narrow retrieval to one corpus category. Leave unset to search everything."
        ),
    )


class SearchKnowledgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    category_filter: Category | None
    passages: list[Passage]


@skill(
    name=NAME,
    description=(
        "Search the clinical reference corpus for background on a condition, mechanism, symptom or "
        "term. Use this for general health questions and whenever no more specific skill fits."
    ),
    category="knowledge",
)
def search_knowledge(args: SearchKnowledgeArgs, ctx: SkillContext) -> SkillResult:
    retriever = require_retriever(ctx, NAME)
    hits = passages(
        retriever.search(args.query, skill=NAME, category=args.category, tracer=ctx.tracer)
    )
    return SkillResult.success(
        NAME,
        SearchKnowledgeResult(query=args.query, category_filter=args.category, passages=hits),
        sources=doc_ids(hits),
    )
