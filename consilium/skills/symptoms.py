"""Skill: ``analyze_symptoms`` -- group a description by body system and name what to investigate.

The output that matters is the single-versus-multi-system distinction.  One system suggests one
organ; several systems at once is either a systemic process or several unrelated complaints, and
which of those it is changes what a clinician would look at next.  The mapping table makes that
distinction computable without the model having to hold anatomy in its head.

**Candidate conditions are retrieved, not inferred.**  They come from the ``condition`` category of
the corpus and are returned as documents to read, never as a ranked differential.  A ranked
differential would be a diagnosis, which ``policy.yaml`` forbids and which this project makes no
claim to be able to produce.

**Unmapped terms are reported, not silently dropped.**  A description whose words are all unmapped
produces ``pattern="unrecognized"`` and an empty grouping, which is an honest "I could not parse
this" rather than a confident "no systems involved".
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
    skill,
)

NAME = "analyze_symptoms"

SymptomPattern = Literal["single-system", "multi-system", "unrecognized"]


class AnalyzeSymptomsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symptoms: str = Field(
        min_length=1, description="The full symptom description, in the user's own words."
    )


class SystemGroup(BaseModel):
    """The terms from one body system that appeared in the description."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: str
    label: str
    terms: list[str]


class CandidateCondition(BaseModel):
    """A corpus note worth reading, not a diagnosis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    title: str


class AnalyzeSymptomsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: SymptomPattern
    systems: list[SystemGroup]
    matched_terms: list[str]
    candidate_conditions: list[CandidateCondition]
    passages: list[Passage]


@skill(
    name=NAME,
    description=(
        "Group a symptom description by body system, report whether the pattern is confined to one "
        "system or spans several, and return corpus notes on conditions worth reading about. "
        "Returns documents to investigate, never a diagnosis or a ranked differential."
    ),
    category="triage",
    requires_retrieval=False,
)
def analyze_symptoms(args: AnalyzeSymptomsArgs, ctx: SkillContext) -> SkillResult:
    if ctx.symptoms is None:
        return SkillResult.failure(NAME, f"{NAME} was invoked without the symptom-system table")

    grouped = ctx.symptoms.group(args.symptoms)
    groups = [
        SystemGroup(system=system, label=ctx.symptoms.label(system), terms=terms)
        for system, terms in grouped.items()
    ]
    matched_terms = sorted({term for terms in grouped.values() for term in terms})

    hits: list[Passage] = []
    if ctx.retriever is not None:
        hits = passages(
            ctx.retriever.search(args.symptoms, skill=NAME, category="condition", tracer=ctx.tracer)
        )

    payload = AnalyzeSymptomsResult(
        pattern=_pattern(len(groups)),
        systems=groups,
        matched_terms=matched_terms,
        candidate_conditions=[
            CandidateCondition(doc_id=hit.doc_id, title=hit.title) for hit in hits
        ],
        passages=hits,
    )
    return SkillResult.success(NAME, payload, sources=doc_ids(hits))


def _pattern(system_count: int) -> SymptomPattern:
    if system_count == 0:
        return "unrecognized"
    return "single-system" if system_count == 1 else "multi-system"
