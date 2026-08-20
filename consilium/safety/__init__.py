"""Safety layer: the rule table, the escalation detector, the policy, the validator, the repair.

Three pieces were built before Phase 7 on purpose.  The red-flag table and its matcher landed in
Phase 2, because a rule table with no loader is unverified and the negation policy needed measuring
before Phase 8 could report it.  The escalation detector landed in Phase 4, because it defines three
fields of the ``turn`` event and ``OutputRepair`` is defined in terms of it rather than the other
way round.  The policy loader landed in Phase 4 too, because ``BaseAgent`` loads its permitted
skills from ``policy.yaml`` at construction.

**Detection and repair are separate classes because they are separate counts.**
``PolicyValidator`` finds violations and records them; ``OutputRepair`` fixes them and records that.
The two rates are reported separately and never merged: one says how often the model produced
non-compliant output, the other how often the guard had to act. A single number would hide a model
getting worse behind a guard that kept working.
"""

from consilium.safety.escalation import (
    ESCALATION_PHRASES,
    escalation_phrases_found,
    escalation_present,
)
from consilium.safety.policy import (
    AgentPolicy,
    EscalationPolicy,
    ForbiddenBehaviour,
    OutputPolicy,
    Policy,
    PolicyError,
    RequiredElement,
)
from consilium.safety.red_flags import (
    NEGATION_CUES,
    NEGATION_WINDOW,
    RedFlagAssessment,
    RedFlagError,
    RedFlagMatch,
    RedFlagRule,
    RedFlagTable,
)
from consilium.safety.repair import OutputRepair, RepairResult
from consilium.safety.validator import (
    UNPERMITTED_SKILL,
    PolicyValidator,
    ToolCallVerdict,
    Violation,
    sentences,
    violation_rules,
)

__all__ = [
    "ESCALATION_PHRASES",
    "NEGATION_CUES",
    "NEGATION_WINDOW",
    "UNPERMITTED_SKILL",
    "AgentPolicy",
    "EscalationPolicy",
    "ForbiddenBehaviour",
    "OutputPolicy",
    "OutputRepair",
    "Policy",
    "PolicyError",
    "PolicyValidator",
    "RedFlagAssessment",
    "RedFlagError",
    "RedFlagMatch",
    "RedFlagRule",
    "RedFlagTable",
    "RepairResult",
    "RequiredElement",
    "ToolCallVerdict",
    "Violation",
    "escalation_phrases_found",
    "escalation_present",
    "sentences",
    "violation_rules",
]
