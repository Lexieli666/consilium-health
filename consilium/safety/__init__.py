"""Safety layer: the red-flag table, the escalation detector, the policy.

``PolicyValidator`` and ``OutputRepair`` land in Phase 7.  The three pieces already here were built
earlier on purpose: the rule table and its matcher in Phase 2, because a rule table with no loader
is unverified and the negation policy needed measuring before Phase 8 could report it; the
escalation detector in Phase 4, because it defines three fields of the ``turn`` event and
``OutputRepair`` is defined in terms of it rather than the other way round; and the policy loader in
Phase 4, because ``BaseAgent`` loads its permitted skills from ``policy.yaml`` at construction.
"""

from consilium.safety.escalation import (
    ESCALATION_PHRASES,
    escalation_phrases_found,
    escalation_present,
)
from consilium.safety.policy import (
    AgentPolicy,
    Policy,
    PolicyError,
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

__all__ = [
    "ESCALATION_PHRASES",
    "NEGATION_CUES",
    "NEGATION_WINDOW",
    "AgentPolicy",
    "Policy",
    "PolicyError",
    "RedFlagAssessment",
    "RedFlagError",
    "RedFlagMatch",
    "RedFlagRule",
    "RedFlagTable",
    "escalation_phrases_found",
    "escalation_present",
]
