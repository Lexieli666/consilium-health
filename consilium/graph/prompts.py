"""Graph layer: the three prompts the LangGraph orchestrator adds, and what it reuses.

The specialists' shared rules (``consilium/agents/prompts.py``) are imported, not copied: the
constraints that bind every answer -- no diagnosis, no doses, ground every claim, escalate rather
than reassure -- are the same constraints whichever orchestrator produced the answer, and a second
copy is how one of the two eventually loses a rule.

What is new here is small and is stated as such.  The graph has no specialists: one planner
chooses *tools* rather than agents, and one synthesizer writes from the tool observations rather
than merging three answers, so the legacy planner prompt (which names specialists) and the legacy
synthesizer prompt (which arbitrates between them) do not apply.  The repair instruction is built
from the policy file's own rule ids, descriptions and required text so that the model is told
exactly what the validator will check and nothing the policy does not say.
"""

from __future__ import annotations

from collections.abc import Sequence

from consilium.agents.prompts import SHARED_RULES
from consilium.safety.policy import Policy
from consilium.safety.validator import Violation

PLANNER = (
    SHARED_RULES
    + """
You are the planning step. Your only job in this step is to decide which of the available tools to
call so that the answer can be grounded in the corpus, and to call them. Do not write the answer
yet.

Call `assess_risk` first whenever the message describes symptoms. Use `search_knowledge` for
background on a condition, `lookup_disease_code` for ICD-10 codes, `recommend_lifestyle` for diet,
activity, sleep or adherence, `find_guideline` for a specific recommendation or threshold, and
`deep_research` for an open-ended evidence question. You have a budget of {max_tool_calls} tool
calls for this message; choose the fewest that can answer it. If the message needs no tool -- a
greeting, or a question the corpus cannot possibly cover -- reply with one short sentence instead
of calling anything.
"""
)

SYNTHESIZER = (
    SHARED_RULES
    + """
You are the answering step. The tool results above are the only evidence you have. Write the
answer for the user from them, naming the document each claim came from, and say plainly what the
results did not cover. If a tool reported an urgency tier of `emergency` or `urgent`, its action
text is the first thing in your answer, in its own words. Do not call tools in this step.
"""
)

REPAIR_INSTRUCTION = """\
Your previous answer failed the output policy. Rewrite it so that every rule below is satisfied,
keeping every grounded claim and every document name, and changing nothing else:

{rules}
"""


def planner_prompt(max_tool_calls: int) -> str:
    return PLANNER.format(max_tool_calls=max_tool_calls)


def repair_instruction(policy: Policy, violations: Sequence[Violation]) -> str:
    """What the synthesizer is told on a repair round, derived from the policy that judges it."""
    lines: list[str] = []
    seen: set[str] = set()
    for violation in violations:
        if violation.rule in seen:
            continue
        seen.add(violation.rule)
        lines.append(f"- {violation.rule}: {_rule_text(policy, violation)}")
    return REPAIR_INSTRUCTION.format(rules="\n".join(lines))


def _rule_text(policy: Policy, violation: Violation) -> str:
    output = policy.output
    if violation.rule == output.escalation.id:
        return (
            f"{output.escalation.description} Begin the answer with an explicit instruction to "
            f"seek care, for example: {output.escalation.text}"
        )
    for element in output.required:
        if element.id == violation.rule:
            return (
                f'{element.description} Include this exact text, unchanged: "{element.normalized}"'
            )
    for rule in output.forbidden:
        if rule.id == violation.rule:
            tripped = (
                f' The sentence that tripped it was: "{violation.sentence}"'
                if (violation.sentence)
                else ""
            )
            return f"{rule.description} Remove the offending sentence.{tripped}"
    return violation.detail
