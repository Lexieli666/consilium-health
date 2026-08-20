"""Agents layer: the general-health specialist, and the system's default."""

from __future__ import annotations

from typing import ClassVar

from consilium.agents.base import BaseAgent
from consilium.agents.prompts import CONSULTATION


class ConsultationAgent(BaseAgent):
    """General health questions, condition explanation, lifestyle guidance, ICD-10 classification.

    Also the fallback: when the planner's JSON fails to parse, the router assigns a single
    ``ConsultationAgent`` subtask and records ``fallback=True``.  It is the right fallback because
    it is the only specialist whose remit has no precondition -- a diagnostic fallback on a coding
    question would answer the wrong question confidently.
    """

    name: ClassVar[str] = "consultation"
    system_prompt: ClassVar[str] = CONSULTATION
