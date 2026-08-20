"""The synthesizer: fixed precedence, and the four guarantees that are code rather than prompt.

"Deterministic, not LLM-arbitrated" is only a claim if something enforces it. What is enforced here
is the ordering, the ownership labels, the missing-perspective note, and the fact that one completed
worker means no merge call at all.
"""

from __future__ import annotations

import pytest

from consilium.agents.base import AgentResult
from consilium.llm import MockProvider, ScriptedResponse
from consilium.router import AGENT_ORDER, ALL_FAILED_ANSWER, PRECEDENCE, Subtask, Synthesizer
from consilium.router.blackboard import SubtaskRecord
from consilium.router.synthesizer import MISSING_PERSPECTIVE_TEMPLATE, owns
from consilium.trace import LLMCallEvent, MemorySink, Tracer
from tests.stubs import FailingProvider


def _record(agent: str, answer: str, *, status: str = "completed", index: int = 1) -> SubtaskRecord:
    subtask = Subtask(
        subtask_id=f"{index}-{agent}", agent=agent, objective=f"the {agent} part", why="test"
    )
    record = SubtaskRecord(subtask=subtask)
    record.status = status  # type: ignore[assignment]
    if status == "completed":
        record.result = AgentResult(
            agent=agent,
            answer=answer,
            sources=(f"doc-{agent}",),
            tool_results=(),
            iterations=1,
            forced=False,
        )
    return record


def _synth(*contents: str) -> Synthesizer:
    return Synthesizer(
        provider=MockProvider([ScriptedResponse(content=content) for content in contents])
    )


def test_precedence_assigns_each_claim_type_to_exactly_one_agent() -> None:
    assert PRECEDENCE == {
        "urgency and red-flag claims": "diagnostic",
        "factual and background claims": "consultation",
        "evidence-strength claims": "research",
    }
    assert set(PRECEDENCE.values()) == set(AGENT_ORDER)
    assert owns("diagnostic") == "urgency and red-flag claims"
    assert owns("nobody") == "no claim type by precedence"


async def test_worker_sections_are_ordered_by_precedence_not_completion() -> None:
    """Completion order is a race; a merge that depended on it would not be reproducible."""
    synth = _synth("Merged.")
    records = [
        _record("research", "evidence", index=1),
        _record("consultation", "background", index=2),
        _record("diagnostic", "urgency", index=3),
    ]

    messages = synth.messages("q", [r for r in records if r.completed][::-1], [])
    body = messages[1].content or ""

    assert body.index("[diagnostic") < body.index("[consultation") < body.index("[research")


async def test_the_prompt_states_the_precedence_and_the_ownership_labels() -> None:
    synth = _synth("Merged.")
    records = [_record("diagnostic", "urgency"), _record("research", "evidence", index=2)]

    system = synth.messages("q", records, [])[0].content or ""
    body = synth.messages("q", records, [])[1].content or ""

    assert "the diagnostic specialist wins" in system
    assert "Never soften it" in system
    assert "[diagnostic -- wins on urgency and red-flag claims]" in body
    assert "sources: doc-diagnostic" in body


async def test_two_completed_workers_are_merged_by_one_synthesizer_call(
    memory_sink: MemorySink,
) -> None:
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)
    synth = _synth("A merged answer.")

    merged = await synth.merge(
        "q",
        completed=[_record("diagnostic", "urgency"), _record("research", "evidence", index=2)],
        missing=[],
        tracer=tracer,
    )

    assert merged == "A merged answer."
    (event,) = memory_sink.of_type(LLMCallEvent)
    assert event.caller == "synthesizer"


async def test_one_completed_worker_is_returned_verbatim_with_no_call(
    memory_sink: MemorySink,
) -> None:
    """Paraphrase is where a grounded answer loses its grounding, and there is nothing to merge."""
    tracer = Tracer(session_id="s", turn_index=0, sink=memory_sink)
    synth = _synth("should not be used")

    merged = await synth.merge(
        "q", completed=[_record("consultation", "the only answer")], missing=[], tracer=tracer
    )

    assert merged == "the only answer"
    assert memory_sink.of_type(LLMCallEvent) == []


async def test_a_missing_perspective_is_named_in_the_delivered_answer() -> None:
    """The model was never shown the failed output, so it cannot be the one to describe it."""
    synth = _synth("Merged from what finished.")

    merged = await synth.merge(
        "q",
        completed=[
            _record("diagnostic", "urgency"),
            _record("consultation", "background", index=2),
        ],
        missing=[_record("research", "", status="timeout", index=3)],
    )

    assert merged.startswith("Merged from what finished.")
    assert MISSING_PERSPECTIVE_TEMPLATE.format(perspectives="research") in merged


async def test_the_missing_note_is_appended_even_on_the_single_worker_path() -> None:
    synth = _synth()

    merged = await synth.merge(
        "q",
        completed=[_record("diagnostic", "urgency")],
        missing=[_record("research", "", status="failed", index=2)],
    )

    assert merged.startswith("urgency")
    assert "research" in merged


async def test_every_worker_failing_still_produces_an_answer() -> None:
    synth = _synth()

    merged = await synth.merge(
        "q",
        completed=[],
        missing=[
            _record("diagnostic", "", status="failed"),
            _record("research", "", status="timeout", index=2),
        ],
    )

    assert merged.startswith(ALL_FAILED_ANSWER)
    assert "diagnostic, research" in merged


async def test_a_failed_merge_falls_back_to_the_workers_own_answers() -> None:
    """Losing grounded answers because the merge failed would be strictly worse than inelegance."""

    synth = Synthesizer(provider=FailingProvider("merge is down"))

    merged = await synth.merge(
        "q",
        completed=[
            _record("diagnostic", "seek care"),
            _record("research", "guidance says", index=2),
        ],
        missing=[],
    )

    assert "diagnostic: seek care" in merged
    assert "research: guidance says" in merged
    assert merged.index("diagnostic") < merged.index("research")


async def test_an_empty_merge_reply_falls_back_the_same_way() -> None:
    synth = _synth("")

    merged = await synth.merge(
        "q",
        completed=[
            _record("diagnostic", "seek care"),
            _record("research", "guidance says", index=2),
        ],
        missing=[],
    )

    assert "diagnostic: seek care" in merged


@pytest.mark.parametrize("agent", AGENT_ORDER)
def test_every_agent_owns_exactly_one_claim_type(agent: str) -> None:
    assert sum(1 for winner in PRECEDENCE.values() if winner == agent) == 1
