"""Agents layer: the symptom and urgency specialist."""

from __future__ import annotations

from typing import ClassVar

from consilium.agents.base import BaseAgent
from consilium.agents.prompts import DIAGNOSTIC


class DiagnosticAgent(BaseAgent):
    """Symptom interpretation, urgency assessment, differential framing.

    Owns urgency and red-flag claims: the synthesizer's fixed precedence defers to this agent on
    them, because a false negative on a red flag is the worst error the system can make and
    over-escalation is the cheaper failure.
    """

    name: ClassVar[str] = "diagnostic"
    system_prompt: ClassVar[str] = DIAGNOSTIC
