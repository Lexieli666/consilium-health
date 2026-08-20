"""Skill: ``assess_risk`` -- map a symptom description to an urgency tier and a required action.

Rule table first, retrieval second.  The urgency tier and the action text come from
``data/red_flags.yaml`` via :class:`~consilium.safety.red_flags.RedFlagTable`; retrieval only adds
the corpus note that explains the presentation.  The order is the point: a model that has to
*summarize retrieved prose* into an urgency tier can summarize it wrongly, whereas a table lookup
returns the same tier for the same input every time, which is what makes red-flag recall a property
of the system rather than of the sampling temperature.

**This skill shares its matcher with ``OutputRepair``.**  Both call the same
:class:`RedFlagTable`, so a phrase that escalates here escalates there.  Two implementations would
show up as an unexplained gap between the safety trigger rate and red-flag recall, and the gap would
be very hard to attribute.

**A "routine" result is not a clearance and says so.**  The table covers a fixed list of
presentations; not matching it means the input did not match the list, not that nothing is wrong.
The distinction is carried in ``action`` rather than left to the model to infer, because the model
inferring it is exactly the failure this skill exists to prevent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from consilium.retrieval.types import Category
from consilium.safety.red_flags import BodySystem, RedFlagAssessment, RedFlagMatch, Urgency
from consilium.skills.base import (
    Passage,
    SkillContext,
    SkillResult,
    passages,
    skill,
)

NAME = "assess_risk"

#: What ``action`` says when nothing in the table matched.  Deliberately not empty and deliberately
#: not reassuring.
NO_MATCH_ACTION = (
    "No emergency pattern in the red-flag table matched this description. That is not a clearance: "
    "the table covers a fixed list of presentations, and a symptom it does not list can still be "
    "serious. If symptoms are severe, worsening, or new and unexplained, seek medical advice."
)


class AssessRiskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symptoms: str = Field(
        min_length=1,
        description="The symptom description, in the user's own words, as completely as possible.",
    )


class MatchedFlag(BaseModel):
    """One red-flag rule that fired, or that fired and was suppressed by the negation guard."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    pattern: str
    body_system: BodySystem
    urgency: Urgency
    doc_id: str
    negated_by: str | None = None


class AssessRiskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    urgency: Urgency
    action: str
    matched: list[MatchedFlag]
    #: Rules that matched the text but were suppressed because an explicit negation cue preceded
    #: them ("no chest pain").  Reported rather than dropped: the caller can see that the phrase was
    #: present and why it did not escalate, which is the difference between a guard and a silent
    #: filter.
    negation_suppressed: list[MatchedFlag]
    passages: list[Passage]


@skill(
    name=NAME,
    description=(
        "Assess how urgently a described symptom needs care. Returns one of routine, non-urgent, "
        "urgent or emergency plus the action the user must take. Call this first for any symptom "
        "description, before explaining anything."
    ),
    category="triage",
    requires_retrieval=False,
)
def assess_risk(args: AssessRiskArgs, ctx: SkillContext) -> SkillResult:
    if ctx.red_flags is None:
        return SkillResult.failure(NAME, f"{NAME} was invoked without the red-flag table")

    assessment = ctx.red_flags.assess(args.symptoms)
    hits = _supporting(args.symptoms, assessment, ctx)

    payload = AssessRiskResult(
        urgency=assessment.urgency,
        action=assessment.action_text() or NO_MATCH_ACTION,
        matched=[_flag(match) for match in assessment.surviving],
        negation_suppressed=[_flag(match) for match in assessment.suppressed],
        passages=hits,
    )
    # Rule doc_ids lead: they are the notes that justify the escalation, and an answer that cites
    # the retrieved prose without the rule's own note cites the weaker of the two sources.
    sources = list(assessment.doc_ids)
    sources += [hit.doc_id for hit in hits if hit.doc_id not in sources]
    return SkillResult.success(NAME, payload, sources=sources)


def _flag(match: RedFlagMatch) -> MatchedFlag:
    """Project a matcher hit onto the skill's own output model.

    A projection rather than a re-export: ``RedFlagMatch`` carries character offsets into the input
    text, which belong in the matcher's tests and not in an observation handed back to a model.
    """
    return MatchedFlag.model_validate(match, from_attributes=True)


def _supporting(symptoms: str, assessment: RedFlagAssessment, ctx: SkillContext) -> list[Passage]:
    """Retrieve the corpus text that explains the assessment.

    Filtered to ``red_flag`` when something fired, unfiltered otherwise.  Filtering in the
    no-match case would search eleven emergency notes for a description that matched none of them
    and return the closest of the eleven, which reads as evidence for an escalation that did not
    happen.
    """
    if ctx.retriever is None:
        return []
    category: Category | None = "red_flag" if assessment.surviving else None
    return passages(
        ctx.retriever.search(symptoms, skill=NAME, category=category, tracer=ctx.tracer)
    )
