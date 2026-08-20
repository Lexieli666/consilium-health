"""Safety: detection.  What the policy forbids, and what it requires.

``PolicyValidator`` **detects and records**; ``OutputRepair`` **fixes**.  They are two classes
because violations and repairs are two counts, reported as two rates and never merged: the violation
rate says how often the model produced non-compliant output, and the repair rate says how often the
guard had to act on it.  A single merged number would hide a model getting worse behind a guard that
kept working.

Two scopes, matching the trace event's ``scope`` field:

``tool_call``  checked **before** execution.  A violation blocks the call.
``output``     checked **after** generation.  A violation triggers a repair.

**Forbidden content is detected per sentence**, not per answer, so the repair can remove exactly the
sentence that tripped and deliver the rest.  Sentence splitting is deliberately simple -- a period,
question mark or exclamation mark followed by whitespace -- because a smarter splitter would be a
second thing to test and the failure mode of the simple one is over-removal of one clause, not
under-detection.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from consilium.safety.escalation import escalation_present
from consilium.safety.policy import Policy
from consilium.safety.red_flags import RedFlagAssessment
from consilium.trace import SafetyScope, Tracer

#: Splits on sentence-ending punctuation followed by whitespace, keeping the punctuation.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

#: The rule id recorded when an agent calls a skill its policy does not permit.
UNPERMITTED_SKILL = "unpermitted_skill"


@dataclass(frozen=True)
class Violation:
    """One detected breach of the policy."""

    rule: str
    scope: SafetyScope
    detail: str
    #: For output violations: the sentence that tripped, so the repair knows what to remove.
    sentence: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class ToolCallVerdict:
    """Whether a tool call may proceed."""

    allowed: bool
    violation: Violation | None = None


def sentences(text: str) -> list[str]:
    """Split an answer into sentences for per-sentence checking."""
    return [part for part in _SENTENCE_RE.split(text.strip()) if part]


class PolicyValidator:
    """Checks tool calls before execution and answers after generation."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def check_tool_call(
        self, *, agent: str, skill: str, tracer: Tracer | None = None
    ) -> ToolCallVerdict:
        """Refuse a skill the agent's policy does not permit.

        The ReAct loop refuses these anyway, without a validator.  Both exist on purpose: the loop's
        refusal is what guarantees an unpermitted skill cannot execute even if the validator is
        absent, and the validator's is what makes the refusal *countable* -- a blocked call with no
        ``safety`` event is a safety mechanism with no measured trigger rate, which is decoration.
        """
        permitted = self.policy.permitted_skills(agent)
        if skill in permitted:
            return ToolCallVerdict(allowed=True)

        violation = Violation(
            rule=UNPERMITTED_SKILL,
            scope="tool_call",
            detail=f"{agent} may not call {skill}; permitted: {', '.join(permitted)}",
            agent=agent,
        )
        _emit(tracer, violation)
        return ToolCallVerdict(allowed=False, violation=violation)

    def check_output(
        self,
        answer: str,
        *,
        assessment: RedFlagAssessment | None = None,
        agent: str | None = None,
        tracer: Tracer | None = None,
    ) -> list[Violation]:
        """Every output violation in ``answer``, in repair order.

        ``assessment`` is the red-flag match on the **user's input**, not on the answer.  Escalation
        is required because of what was asked, and an answer that never mentions the symptom would
        otherwise pass by saying nothing.
        """
        found: list[Violation] = []
        found.extend(self._forbidden(answer, agent))
        found.extend(self._escalation(answer, assessment, agent))
        found.extend(self._required(answer, agent))

        for violation in found:
            _emit(tracer, violation)
        return found

    def _forbidden(self, answer: str, agent: str | None) -> list[Violation]:
        found: list[Violation] = []
        for sentence in sentences(answer):
            for rule in self.policy.output.forbidden:
                pattern = rule.matches(sentence)
                if pattern is None:
                    continue
                found.append(
                    Violation(
                        rule=rule.id,
                        scope="output",
                        detail=f"matched /{pattern}/ in: {sentence[:160]}",
                        sentence=sentence,
                        agent=agent,
                    )
                )
                break  # one violation per sentence; the sentence is removed either way
        return found

    def _escalation(
        self, answer: str, assessment: RedFlagAssessment | None, agent: str | None
    ) -> list[Violation]:
        if assessment is None or not assessment.matched:
            return []
        if escalation_present(answer):
            return []
        rule = self.policy.output.escalation
        rules = ", ".join(match.rule_id for match in assessment.surviving)
        return [
            Violation(
                rule=rule.id,
                scope="output",
                detail=f"input matched {rules} and the answer contains no seek-care instruction",
                agent=agent,
            )
        ]

    def _required(self, answer: str, agent: str | None) -> list[Violation]:
        normalized = " ".join(answer.split())
        return [
            Violation(
                rule=element.id,
                scope="output",
                detail=f"the answer does not carry the required {element.id}",
                agent=agent,
            )
            for element in self.policy.output.required
            if element.normalized not in normalized
        ]


def _emit(tracer: Tracer | None, violation: Violation) -> None:
    if tracer is None:
        return
    tracer.safety(
        event="violation",
        rule=violation.rule,
        scope=violation.scope,
        agent=violation.agent,
        detail=violation.detail,
    )


def violation_rules(violations: Sequence[Violation]) -> tuple[str, ...]:
    """Rule ids, deduplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for violation in violations:
        seen.setdefault(violation.rule, None)
    return tuple(seen)
