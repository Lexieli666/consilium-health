"""Router layer: merging worker outputs into one answer, with fixed precedence.

**Conflict precedence is deterministic, not LLM-arbitrated.**  Urgency and red-flag claims defer to
``DiagnosticAgent``, factual and background claims to ``ConsultationAgent``, evidence-strength
claims to ``ResearchAgent``.  "Ask the model which answer is better" was the rejected alternative,
and it fails on the case that matters most: the one where the diagnostic agent says *seek care now*
and another agent's calmer, better-written paragraph reads more authoritative.  A false negative on
a red flag is the worst error this system can make and over-escalation is the cheaper failure, so
that particular contest is not one the model gets to hold.

Four things here are code, not prompt text, and they are what makes "deterministic" a claim rather
than an aspiration:

1. **Worker outputs are presented in precedence order**, never in completion order.  Completion
   order is a race, and a merge that depended on it would synthesize the same question differently
   on different runs -- measured, later, as architecture.
2. **Each section is labelled with what its agent owns**, so the instruction and the evidence cannot
   drift apart.
3. **Missing perspectives are named in the delivered answer by code**, appended after the merge.
   The brief requires the answer to say which perspective is missing; leaving that to a model that
   was not given the failed output is asking it to describe something it cannot see.
4. **One completed worker means no synthesizer call at all.**  There is nothing to merge, the
   second call would cost tokens and latency to paraphrase one answer, and paraphrasing is the step
   at which a grounded answer loses its grounding.

The risk level of a turn is never set here.  It comes from the red-flag table applied to the user's
input, in ``consilium/runtime.py``, so no amount of merging can move it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from consilium.llm.base import LLMProvider, Message
from consilium.log import get_logger
from consilium.router.blackboard import SubtaskRecord
from consilium.trace import Tracer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from consilium.agents.base import AgentResult

log = get_logger(__name__)

#: Which specialist wins on which kind of claim.  The order of this mapping is the order worker
#: sections are presented in, so it is a ranking and not just a lookup.
PRECEDENCE: dict[str, str] = {
    "urgency and red-flag claims": "diagnostic",
    "factual and background claims": "consultation",
    "evidence-strength claims": "research",
}

#: Presentation order for worker sections: precedence first, then anything unrecognized.
AGENT_ORDER: tuple[str, ...] = ("diagnostic", "consultation", "research")

#: Appended by code when a subtask did not complete.  The brief requires the answer to say which
#: perspective is missing, and the model cannot describe an output it was never shown.
MISSING_PERSPECTIVE_TEMPLATE = (
    "\n\nNote: the {perspectives} perspective could not be completed for this question, "
    "so this answer may be incomplete in that respect."
)

#: Delivered when every worker failed.  The user always gets a response.
ALL_FAILED_ANSWER = (
    "I was not able to complete any part of this question. If this concerns a symptom that is "
    "severe, worsening, or new and unexplained, seek medical advice."
)

SYSTEM_PROMPT = """\
You merge the answers of several specialists into one answer for the user.

You are not a fourth specialist. You add no facts, no claims and no reassurance of your own. Every
sentence you write must be traceable to one of the specialist answers below, and you keep the
document names they cite.

Where two specialists disagree, this order decides, and it is not yours to weigh:

- urgency and red-flag claims: the diagnostic specialist wins.
- factual and background claims: the consultation specialist wins.
- evidence-strength claims: the research specialist wins.

If the diagnostic specialist says the user should seek care, that instruction comes first in your
answer, in its own words, before anything else. Never soften it, never move it later, and never
balance it against a calmer statement from another specialist.

Where guidance genuinely differs between authorities, say so as a disagreement. Do not pick a side
and do not average two numbers into one neither authority published.

Write a few short paragraphs for a reader with no clinical training.
"""


class Synthesizer:
    """Merges completed worker outputs into the delivered answer."""

    def __init__(self, *, provider: LLMProvider) -> None:
        self.provider = provider

    async def merge(
        self,
        question: str,
        *,
        completed: Sequence[SubtaskRecord],
        missing: Sequence[SubtaskRecord],
        tracer: Tracer | None = None,
    ) -> str:
        """Return the delivered answer for a parallel turn."""
        ordered = order_by_precedence(completed)

        if not ordered:
            return ALL_FAILED_ANSWER + missing_note(missing)

        if len(ordered) == 1:
            # Nothing to merge.  A second call here would spend tokens paraphrasing one grounded
            # answer, and paraphrase is where grounding is lost.
            return (ordered[0].result.answer if ordered[0].result else "") + missing_note(missing)

        try:
            response = await self.provider.chat(
                self.messages(question, ordered, missing),
                tools=None,
                tracer=tracer,
                caller="synthesizer",
            )
        except Exception as exc:  # the merge failing must not lose the workers' answers
            log.exception("synthesizer.provider_failed")
            return concatenate(ordered) + missing_note(missing, extra=f"merge failed: {exc}")

        merged = (response.content or "").strip()
        if not merged:
            log.warning("synthesizer.empty_reply")
            return concatenate(ordered) + missing_note(missing)
        return merged + missing_note(missing)

    def messages(
        self,
        question: str,
        ordered: Sequence[SubtaskRecord],
        missing: Sequence[SubtaskRecord],
    ) -> list[Message]:
        sections = "\n\n".join(section(record) for record in ordered)
        absent = (
            ""
            if not missing
            else "\n\nThese specialists did not complete: "
            + ", ".join(sorted({record.subtask.agent for record in missing}))
            + ". Do not invent what they would have said."
        )
        return [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"The user asked:\n{question}\n\n"
                    f"Specialist answers, in precedence order:\n\n{sections}{absent}"
                ),
            ),
        ]


def order_by_precedence(records: Sequence[SubtaskRecord]) -> list[SubtaskRecord]:
    """Completed records in fixed precedence order, never in completion order."""
    completed = [record for record in records if record.completed]
    return sorted(
        completed,
        key=lambda record: (
            AGENT_ORDER.index(record.subtask.agent)
            if record.subtask.agent in AGENT_ORDER
            else len(AGENT_ORDER),
            record.subtask.subtask_id,
        ),
    )


def owns(agent: str) -> str:
    """What this agent's claims win on, for the section label."""
    for claim, winner in PRECEDENCE.items():
        if winner == agent:
            return claim
    return "no claim type by precedence"


def section(record: SubtaskRecord) -> str:
    result: AgentResult | None = record.result
    answer = result.answer if result else ""
    sources = ", ".join(result.sources) if result and result.sources else "none"
    return (
        f"[{record.subtask.agent} -- wins on {owns(record.subtask.agent)}]\n"
        f"objective: {record.subtask.objective}\n"
        f"sources: {sources}\n"
        f"{answer}"
    )


def concatenate(ordered: Sequence[SubtaskRecord]) -> str:
    """The fallback merge: the workers' own answers, in precedence order, unaltered.

    Used when the synthesizer call fails or returns nothing.  Losing the workers' grounded answers
    because the merge failed would be a strictly worse outcome than an inelegant one.
    """
    return "\n\n".join(
        f"{record.subtask.agent}: {record.result.answer}" for record in ordered if record.result
    )


def missing_note(missing: Sequence[SubtaskRecord], *, extra: str = "") -> str:
    if not missing:
        return ""
    perspectives = ", ".join(sorted({record.subtask.agent for record in missing}))
    note = MISSING_PERSPECTIVE_TEMPLATE.format(perspectives=perspectives)
    return f"{note} ({extra})" if extra else note
