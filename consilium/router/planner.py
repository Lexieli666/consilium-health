"""Router layer: one LLM call that decides which specialists answer.

**Planner-based routing, not a trained classifier and not keyword rules.**  Rationale, with both
rejected alternatives, is in docs/DESIGN.md.

**Every way the plan can be unusable produces the same fallback, and the fallback is counted.**
No content, no JSON in the content, JSON that does not parse, JSON that fails the schema, a plan
naming an agent that does not exist, an empty plan, or a provider error -- all of them assign a
single ``ConsultationAgent`` subtask and set ``fallback=True`` on the ``route`` event.  That flag is
a reported metric: routing accuracy is reported both unconditionally (fallbacks counted as their
effective behaviour) and excluding fallback turns, and the second number without the first would let
a planner that fails half the time look perfect.

**An unknown agent name invalidates the whole plan rather than being dropped.**  Dropping it would
silently produce a smaller plan than the planner intended, and the turn would look like a successful
route to a narrower set of agents -- which is a wrong routing decision recorded as a right one.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from consilium.llm.base import LLMProvider, Message
from consilium.log import get_logger
from consilium.router.plan import Plan, PlanItem, Subtask, number
from consilium.safety.policy import Policy
from consilium.trace import Tracer

log = get_logger(__name__)

#: The fallback specialist.  The only one whose remit has no precondition: a diagnostic fallback on
#: a coding question would answer the wrong question confidently.
FALLBACK_AGENT = "consultation"

FALLBACK_OBJECTIVE = (
    "Answer the user's question directly from the corpus, and say what you could not cover."
)

SYSTEM_PROMPT = """\
You are the planner for Consilium Health, a clinical-information assistant with three specialists.

Your only job is to decide which specialists should answer a question, and to say what each one
should do. You never answer the question yourself.

Assign the FEWEST specialists that can answer the question. Most questions need exactly one. Assign
two or three only when the question genuinely has parts that different specialists own -- for
example a symptom that needs urgency assessment AND a guideline question about the same condition.
Never assign the same specialist twice.

Reply with JSON only, in exactly this shape, and nothing else:

{"subtasks": [{"agent": "<name>", "objective": "<what this specialist should do>", "why": "<one
short sentence>"}]}
"""

FEW_SHOT = """\
Examples.

Question: "What is the ICD-10 code for type 2 diabetes?"
{"subtasks": [{"agent": "consultation", "objective": "Give the ICD-10 code for type 2 diabetes and
the convention that governs choosing it.", "why": "A classification question with no symptom and no
evidence component."}]}

Question: "I have had a headache and a stiff neck since this morning."
{"subtasks": [{"agent": "diagnostic", "objective": "Assess how urgently these symptoms need care
and group them by body system.", "why": "A symptom description, which is an urgency question
first."}]}

Question: "My blood pressure is 150/95. Is that bad, and what do the guidelines say the target
should be?"
{"subtasks": [{"agent": "diagnostic", "objective": "Assess the urgency of this blood pressure
reading.", "why": "The reading has to be triaged before anything else."}, {"agent": "research",
"objective": "Report what guidance says about blood pressure targets, including where authorities
differ.", "why": "The second half is explicitly a guideline question."}]}
"""


class Planner:
    """One LLM call, one validated plan, one counted fallback when it cannot be had."""

    def __init__(self, *, provider: LLMProvider, policy: Policy) -> None:
        self.provider = provider
        self.policy = policy
        self.known_agents: tuple[str, ...] = tuple(sorted(policy))

    def capability_block(self) -> str:
        """The three capability descriptions, read from ``policy.yaml``.

        Read from the policy rather than written into this prompt: one description, so the planner
        cannot be told an agent does something the policy does not permit it to do.
        """
        return "\n".join(f"- {name}: {self.policy.description(name)}" for name in self.known_agents)

    def messages(self, question: str) -> list[Message]:
        return [
            Message(
                role="system",
                content=(
                    f"{SYSTEM_PROMPT}\n"
                    f"The specialists are:\n{self.capability_block()}\n\n{FEW_SHOT}"
                ),
            ),
            Message(role="user", content=question),
        ]

    async def plan(
        self, question: str, *, tracer: Tracer | None = None
    ) -> tuple[list[Subtask], bool]:
        """Return ``(subtasks, fallback)``.  Never raises."""
        try:
            response = await self.provider.chat(
                self.messages(question), tools=None, tracer=tracer, caller="planner"
            )
        except Exception as exc:  # a provider outage must not end the turn
            log.exception("planner.provider_failed")
            return self.fallback(f"{type(exc).__name__}: {exc}"), True

        parsed = self.parse(response.content)
        if parsed is None:
            return self.fallback("unusable plan"), True
        return parsed, False

    def parse(self, content: str | None) -> list[Subtask] | None:
        """Validate the planner's reply into subtasks, or ``None`` if it cannot be used."""
        if not content:
            log.warning("planner.empty_reply")
            return None

        raw = extract_json_object(content)
        if raw is None:
            log.warning("planner.no_json", reply=content[:200])
            return None

        try:
            plan = Plan.model_validate_json(raw)
        except ValidationError as exc:
            log.warning("planner.invalid_plan", error=str(exc)[:200])
            return None

        unknown = [item.agent for item in plan.subtasks if item.agent not in self.known_agents]
        if unknown:
            log.warning("planner.unknown_agent", agents=unknown)
            return None

        return number(dedupe(plan.subtasks))

    def fallback(self, reason: str) -> list[Subtask]:
        log.warning("planner.fallback", reason=reason)
        return number([PlanItem(agent=FALLBACK_AGENT, objective=FALLBACK_OBJECTIVE, why=reason)])


def dedupe(items: Sequence[PlanItem]) -> list[PlanItem]:
    """Keep the first subtask per agent.

    Two subtasks for one specialist buy one perspective at twice the cost, and they break the
    routing metric outright: ``route.agents`` is compared against a labelled agent list, and a
    repeated name would never match it.
    """
    seen: set[str] = set()
    kept: list[PlanItem] = []
    for item in items:
        if item.agent in seen:
            continue
        seen.add(item.agent)
        kept.append(item)
    return kept


def extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` in ``text``, ignoring braces inside strings.

    Models wrap JSON in prose and in code fences.  A regex would either miss the nested object in
    ``{"subtasks": [{...}]}`` or match greedily past the end of it; a brace counter that knows about
    string literals and escapes is short, exact, and does not need the model to behave.
    """
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
