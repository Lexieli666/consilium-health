"""Working memory and context compaction: the window, the dedup, and the deterministic recap.

Called context compaction here and everywhere else in the project. Nothing in this module computes
an entropy, so nothing calls it entropy management.
"""

from __future__ import annotations

import pytest

from consilium.memory import (
    RECAP_PREFIX,
    SOURCES_PREFIX,
    WINDOW_EXCHANGES,
    SessionState,
    WorkingMemory,
    digest,
)
from consilium.skills import SkillResult


def _result(skill: str, payload: str, *sources: str) -> SkillResult:
    return SkillResult(skill=skill, ok=True, data={"text": payload}, sources=sources)


def _memory(turns: int = 0, *, window: int = WINDOW_EXCHANGES) -> WorkingMemory:
    memory = WorkingMemory("session-a", window=window)
    for index in range(turns):
        memory.record(
            question=f"question {index}",
            answer=f"Answer {index}. A second sentence that the recap should drop.",
            tool_results=[_result("search_knowledge", f"passage {index}", f"doc-{index}")],
        )
    return memory


def test_a_fresh_session_has_no_history() -> None:
    memory = _memory()
    assert memory.history() == []
    assert memory.recap() is None
    assert len(memory) == 0


def test_history_replays_the_window_as_alternating_messages() -> None:
    memory = _memory(3)
    history = memory.history()

    assert [message.role for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert history[0].content == "question 0"
    assert history[1].content is not None and history[1].content.startswith("Answer 0.")


def test_provenance_survives_into_the_replayed_answer() -> None:
    memory = _memory(1)
    (_, assistant) = memory.history()

    assert assistant.content is not None
    assert f"{SOURCES_PREFIX}doc-0]" in assistant.content


def test_exchanges_beyond_the_window_are_compacted_not_dropped() -> None:
    memory = _memory(WINDOW_EXCHANGES + 3)

    assert len(memory) == WINDOW_EXCHANGES + 3
    assert len(memory.windowed()) == WINDOW_EXCHANGES
    assert len(memory.compacted()) == 3

    history = memory.history()
    assert history[0].role == "user"
    assert history[0].content is not None and history[0].content.startswith(RECAP_PREFIX)
    assert len(history) == 1 + 2 * WINDOW_EXCHANGES


def test_the_recap_is_delivered_as_a_user_message_not_a_system_one() -> None:
    """System content is instruction, and Anthropic lifts it into the agent's own rules."""
    memory = _memory(WINDOW_EXCHANGES + 1)
    assert memory.history()[0].role == "user"


def test_the_recap_keeps_the_question_the_opening_sentence_and_the_sources() -> None:
    memory = _memory(WINDOW_EXCHANGES + 2)
    recap = memory.recap()

    assert recap is not None
    assert "question 0" in recap
    assert "Answer 0." in recap
    assert "A second sentence that the recap should drop." not in recap
    assert "Documents already consulted: doc-0, doc-1." in recap


def test_the_recap_notes_a_non_routine_risk_level() -> None:
    memory = WorkingMemory("session-a", window=1)
    memory.record(question="chest pain", answer="Call emergency services.", risk_level="emergency")
    memory.record(question="and now", answer="Anything.")

    recap = memory.recap()
    assert recap is not None
    assert "risk level recorded: emergency" in recap


def test_the_recap_is_deterministic() -> None:
    """An LLM summarizer would put a nondeterministic string into every later turn's input."""
    assert _memory(WINDOW_EXCHANGES + 2).recap() == _memory(WINDOW_EXCHANGES + 2).recap()


def test_identical_observations_are_deduplicated_by_content_hash() -> None:
    memory = _memory()
    same = _result("search_knowledge", "the same passage", "doc-a")

    memory.record(question="q1", answer="a1", tool_results=[same])
    memory.record(question="q2", answer="a2", tool_results=[same])

    assert memory.duplicates_dropped == 1
    assert len(memory.exchanges[0].observations) == 1
    assert memory.exchanges[1].observations == ()


def test_different_observations_are_both_kept() -> None:
    memory = _memory()
    memory.record(question="q1", answer="a1", tool_results=[_result("s", "one", "doc-a")])
    memory.record(question="q2", answer="a2", tool_results=[_result("s", "two", "doc-b")])

    assert memory.duplicates_dropped == 0
    assert memory.exchanges[1].observations[0].sources == ("doc-b",)


def test_failed_tool_calls_are_not_carried_forward() -> None:
    """A failed call observed nothing; recording it would put an error string in the recap."""
    memory = _memory()
    memory.record(
        question="q",
        answer="a",
        tool_results=[SkillResult.failure("search_knowledge", "boom")],
    )

    assert memory.exchanges[0].observations == ()


def test_the_content_digest_is_stable_rather_than_salted_per_process() -> None:
    """`hash()` is randomized per process; the same session must compact identically anywhere."""
    # A golden value, computed once in a different process. That is the property `hash()` lacks:
    # it is salted per interpreter run, so an equality check inside one process proves nothing.
    assert digest("a passage") == "68b14c8f7a151e50b900bbfa22ccc342"
    assert digest("a passage") != digest("another passage")


def test_tool_observations_are_never_replayed_as_messages() -> None:
    """Replaying them would grow the context without bound and void the turn's tool budget."""
    memory = _memory(2)
    contents = [message.content or "" for message in memory.history()]

    assert not any("passage 0" in content for content in contents)
    assert any(
        "doc-0" in content for content in contents
    )  # the citation survives, the text does not


def test_a_session_round_trips_through_json() -> None:
    memory = _memory(WINDOW_EXCHANGES + 1)
    restored = WorkingMemory.from_json(memory.to_json())

    assert restored.session_id == memory.session_id
    assert len(restored) == len(memory)
    assert restored.history() == memory.history()
    assert restored.duplicates_dropped == memory.duplicates_dropped


def test_clearing_a_session_resets_it_without_changing_its_identity() -> None:
    memory = _memory(3)
    memory.clear()

    assert len(memory) == 0
    assert memory.session_id == "session-a"
    assert memory.history() == []


def test_a_window_of_one_still_compacts_correctly() -> None:
    memory = _memory(3, window=1)

    assert len(memory.windowed()) == 1
    assert len(memory.compacted()) == 2


def test_a_nonpositive_window_is_refused() -> None:
    with pytest.raises(ValueError, match="window must be >= 1"):
        WorkingMemory("s", window=0)


def test_the_session_state_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="invented"):
        SessionState.model_validate({"session_id": "s", "invented": 1})
