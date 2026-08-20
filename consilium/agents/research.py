"""Agents layer: the guideline and evidence specialist."""

from __future__ import annotations

from typing import ClassVar

from consilium.agents.base import BaseAgent
from consilium.agents.prompts import RESEARCH


class ResearchAgent(BaseAgent):
    """Guideline lookup, evidence synthesis, "what does the literature say" questions.

    Owns evidence-strength claims under the synthesizer's fixed precedence.
    """

    name: ClassVar[str] = "research"
    system_prompt: ClassVar[str] = RESEARCH
