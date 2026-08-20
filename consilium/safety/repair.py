"""Safety: repair.  Turning a detected violation into a deliverable answer.

``OutputRepair`` consumes the violations ``PolicyValidator`` found and returns the text that is
actually delivered, emitting one ``repair`` trace event per rule it acted on.

**The repair order is fixed**, and each position is load-bearing:

1. **Redact** forbidden sentences.  Removed, never rephrased: rewriting a clinical sentence into a
   different clinical sentence produces text nobody wrote and nobody checked, and the rewrite would
   itself need checking.  What is left in its place is a marker naming the rule, so the reader can
   see that something was removed rather than silently receiving a gap.
2. **Prepend** the escalation banner.  First, because it is the sentence that must be read first.
3. **Append** the disclaimer.  Last, because it is boilerplate.

**The banner is prepended only when the answer lacks a seek-care instruction.**  That is why a
correctly-handled red flag emits no repair event at all, and why measuring red-flag recall from
repair events would score a good answer as a false negative.  The ``turn`` event carries
``escalation_present_pre_repair``, ``escalation_present_post_repair`` and ``repair_applied``
precisely so those three cases stay distinguishable.

**``post_stream`` marks a repair applied after tokens had already reached the client.**  Only the
SSE path can set it: ``POST /v1/chat`` streams the escalation banner *before* the model's tokens
when the input matches a red flag, so the input-side case never needs it -- but an output-side
violation detected after the stream finished is a repair the user has already seen the unrepaired
version of.  ``docs/SAFETY.md`` is required to state this plainly, and the flag exists so the
statement can be quantified rather than asserted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from consilium.safety.escalation import escalation_present
from consilium.safety.policy import Policy
from consilium.safety.validator import Violation
from consilium.trace import Tracer


@dataclass(frozen=True)
class RepairResult:
    """The delivered answer, and everything the ``turn`` event needs to record about it."""

    answer: str
    violations: tuple[Violation, ...] = ()
    repairs: tuple[str, ...] = ()
    escalation_present_pre_repair: bool = False
    escalation_present_post_repair: bool = False
    redacted: tuple[str, ...] = field(default_factory=tuple)

    @property
    def repair_applied(self) -> bool:
        return bool(self.repairs)


class OutputRepair:
    """Applies the policy's repairs to an answer, in the fixed order above."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def apply(
        self,
        answer: str,
        violations: Sequence[Violation],
        *,
        agent: str | None = None,
        tracer: Tracer | None = None,
        post_stream: bool = False,
    ) -> RepairResult:
        """Repair ``answer`` for the violations found in it."""
        pre = escalation_present(answer)
        repaired = answer
        applied: list[str] = []
        redacted: list[str] = []

        forbidden_ids = {rule.id for rule in self.policy.output.forbidden}
        for violation in violations:
            if violation.rule in forbidden_ids and violation.sentence:
                rule = next(r for r in self.policy.output.forbidden if r.id == violation.rule)
                if violation.sentence in repaired:
                    repaired = repaired.replace(violation.sentence, rule.replacement)
                    redacted.append(violation.sentence)
                    applied.append(rule.id)
                    _emit(
                        tracer,
                        rule.id,
                        agent,
                        f"redacted: {violation.sentence[:160]}",
                        post_stream,
                    )

        escalation = self.policy.output.escalation
        if any(violation.rule == escalation.id for violation in violations):
            repaired = f"{escalation.text}\n\n{repaired}"
            applied.append(escalation.id)
            _emit(tracer, escalation.id, agent, "prepended the escalation banner", post_stream)

        for element in self.policy.output.required:
            if not any(violation.rule == element.id for violation in violations):
                continue
            repaired = (
                f"{repaired.rstrip()}\n\n{element.normalized}"
                if element.repair == "append"
                else f"{element.normalized}\n\n{repaired.lstrip()}"
            )
            applied.append(element.id)
            _emit(tracer, element.id, agent, f"{element.repair}ed the {element.id}", post_stream)

        return RepairResult(
            answer=repaired,
            violations=tuple(violations),
            repairs=tuple(applied),
            escalation_present_pre_repair=pre,
            escalation_present_post_repair=escalation_present(repaired),
            redacted=tuple(redacted),
        )


def _emit(
    tracer: Tracer | None, rule: str, agent: str | None, detail: str, post_stream: bool
) -> None:
    if tracer is None:
        return
    tracer.safety(
        event="repair",
        rule=rule,
        scope="output",
        agent=agent,
        detail=detail,
        post_stream=post_stream,
    )
