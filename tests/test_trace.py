"""The trace schema is the artifact every reported number is computed from, so it is tested as an
interface: each event type must survive a write/read round trip, and malformed records must fail
loudly rather than be silently skipped by the evaluation harness.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from consilium.trace import (
    SCHEMA_VERSION,
    BlackboardEvent,
    FusedHit,
    MemorySink,
    PlannedSubtask,
    RetrievalEvent,
    TraceError,
    Tracer,
    TurnEvent,
    parse_event,
    read_trace,
    stopwatch,
    trace_path,
)

ALL_EVENT_TYPES = {
    "route",
    "llm_call",
    "tool_call",
    "retrieval",
    "safety",
    "blackboard",
    "turn",
}


def emit_one_of_each(tracer: Tracer) -> None:
    tracer.route(
        mode="parallel",
        agents=["diagnostic", "research"],
        subtasks=[
            PlannedSubtask(
                subtask_id="s1",
                agent="diagnostic",
                objective="assess urgency",
                why="the question describes symptoms",
            )
        ],
        fallback=False,
        latency_ms=12.5,
    )
    tracer.llm_call(
        caller="agent:diagnostic",
        provider="mock",
        model="mock-model",
        prompt_tokens=120,
        completion_tokens=40,
        latency_ms=88.0,
        tools_offered=["assess_risk"],
        stop_reason="tool_calls",
    )
    tracer.tool_call(
        agent="diagnostic",
        skill="assess_risk",
        args={"symptoms": "chest pain"},
        ok=True,
        error=None,
        latency_ms=3.0,
        source_doc_ids=["red-flag-chest-pain"],
    )
    tracer.retrieval(
        skill="assess_risk",
        query="chest pain",
        category_filter="red_flag",
        fused_topk=[FusedHit(doc_id="red-flag-chest-pain", chunk_index=0, rrf_score=0.032)],
        returned_k=5,
        latency_ms=6.0,
    )
    tracer.safety(
        event="repair",
        rule="escalation_banner",
        scope="output",
        detail="answer lacked a seek-care instruction",
        agent="diagnostic",
    )
    tracer.blackboard(event="completed", subtask_id="s1", agent="diagnostic")
    tracer.turn(
        question="I have chest pain",
        answer="Seek emergency care now.",
        risk_level="emergency",
        wall_ms=1500.0,
        red_flag_matched=True,
        escalation_present_pre_repair=False,
        escalation_present_post_repair=True,
        repair_applied=True,
    )


def test_every_event_type_round_trips(tracer: Tracer, memory_sink: MemorySink) -> None:
    emit_one_of_each(tracer)

    assert {event.type for event in memory_sink.events} == ALL_EVENT_TYPES
    for event in memory_sink.events:
        reparsed = parse_event(event.model_dump_json())
        assert reparsed == event


def test_every_record_carries_schema_version_and_turn_identity(
    tracer: Tracer, memory_sink: MemorySink
) -> None:
    emit_one_of_each(tracer)

    for event in memory_sink.events:
        payload = json.loads(event.model_dump_json())
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["trace_id"] == "trace-0001"
        assert payload["session_id"] == "test-session"
        assert payload["turn_index"] == 0
        assert payload["ts"].startswith("2026-01-01T12:00:00")


def test_turn_event_carries_the_three_escalation_fields(
    tracer: Tracer, memory_sink: MemorySink
) -> None:
    """Red-flag recall is defined on the delivered answer, not on the repair event."""
    event = tracer.turn(
        question="I have crushing chest pain",
        answer="Call emergency services now.",
        risk_level="emergency",
        wall_ms=10.0,
        red_flag_matched=True,
        escalation_present_pre_repair=True,
        escalation_present_post_repair=True,
        repair_applied=False,
    )
    payload = json.loads(event.model_dump_json())

    assert payload["escalation_present_pre_repair"] is True
    assert payload["escalation_present_post_repair"] is True
    assert payload["repair_applied"] is False
    assert memory_sink.of_type(TurnEvent) == [event]


def test_retrieval_event_keeps_the_full_fused_ranking(
    tracer: Tracer, memory_sink: MemorySink
) -> None:
    """MRR@10 needs ranks 6-10, which the model never sees."""
    hits = [FusedHit(doc_id=f"doc-{i}", chunk_index=0, rrf_score=1.0 / (60 + i)) for i in range(10)]
    tracer.retrieval(
        skill="search_knowledge",
        query="statin therapy",
        category_filter=None,
        fused_topk=hits,
        returned_k=5,
        latency_ms=4.0,
    )

    event = memory_sink.of_type(RetrievalEvent)[0]
    payload = json.loads(event.model_dump_json())
    assert len(payload["fused_topk"]) == 10
    assert payload["returned_k"] == 5
    assert [hit["doc_id"] for hit in payload["fused_topk"]] == [f"doc-{i}" for i in range(10)]


@pytest.mark.parametrize(
    "caller",
    ["planner", "synthesizer", "forced_answer", "agent:consultation", "agent:deep_research"],
)
def test_valid_caller_labels_are_accepted(tracer: Tracer, caller: str) -> None:
    assert (
        tracer.llm_call(
            caller=caller,
            provider="mock",
            model="mock-model",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=0.0,
            tools_offered=[],
            stop_reason="stop",
        ).caller
        == caller
    )


@pytest.mark.parametrize("caller", ["Planner", "agent:", "agent:Consultation", "router", ""])
def test_malformed_caller_labels_are_rejected(tracer: Tracer, caller: str) -> None:
    """A typo in a caller label would silently create a new bucket in tokens-per-caller."""
    with pytest.raises(ValidationError):
        tracer.llm_call(
            caller=caller,
            provider="mock",
            model="mock-model",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=0.0,
            tools_offered=[],
            stop_reason="stop",
        )


def test_unknown_event_type_is_rejected() -> None:
    line = json.dumps(
        {
            "schema_version": 1,
            "ts": "2026-01-01T12:00:00Z",
            "trace_id": "t",
            "session_id": "s",
            "turn_index": 0,
            "type": "vibes",
        }
    )
    with pytest.raises(ValidationError):
        parse_event(line)


def test_extra_fields_are_rejected() -> None:
    line = json.dumps(
        {
            "schema_version": 1,
            "ts": "2026-01-01T12:00:00Z",
            "trace_id": "t",
            "session_id": "s",
            "turn_index": 0,
            "type": "blackboard",
            "event": "started",
            "subtask_id": "s1",
            "agent": "diagnostic",
            "undocumented": "value",
        }
    )
    with pytest.raises(ValidationError):
        parse_event(line)


def test_negative_latency_is_rejected(tracer: Tracer) -> None:
    with pytest.raises(ValidationError):
        tracer.route(
            mode="single", agents=["consultation"], subtasks=[], fallback=False, latency_ms=-1.0
        )


def test_tracer_writes_jsonl_to_the_expected_path(tmp_path: Path) -> None:
    with Tracer.for_turn(session_id="sess-1", turn_index=3, runs_dir=tmp_path) as tracer:
        emit_one_of_each(tracer)

    path = trace_path(tmp_path, "sess-1", 3)
    assert path == tmp_path / "sess-1" / "3.jsonl"
    assert path.exists()

    events = read_trace(path)
    assert [event.type for event in events] == [
        "route",
        "llm_call",
        "tool_call",
        "retrieval",
        "safety",
        "blackboard",
        "turn",
    ]


def test_trace_is_readable_before_the_turn_finishes(tmp_path: Path) -> None:
    """A turn that crashed is exactly the turn whose trace matters, so lines are flushed."""
    tracer = Tracer.for_turn(session_id="sess-2", turn_index=0, runs_dir=tmp_path)
    tracer.blackboard(event="assigned", subtask_id="s1", agent="research")

    assert len(read_trace(trace_path(tmp_path, "sess-2", 0))) == 1
    tracer.close()


def test_malformed_line_reports_its_line_number(tmp_path: Path) -> None:
    path = tmp_path / "sess-3" / "0.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"not": "an event"}\n', encoding="utf-8")

    with pytest.raises(TraceError, match=r":1: invalid trace record"):
        read_trace(path)


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "", "with space", "x" * 65, ".hidden"])
def test_session_ids_that_would_escape_the_runs_directory_are_rejected(
    session_id: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="session_id"):
        Tracer.for_turn(session_id=session_id, turn_index=0, runs_dir=tmp_path)


def test_negative_turn_index_is_rejected(memory_sink: MemorySink) -> None:
    with pytest.raises(ValueError, match="turn_index"):
        Tracer(session_id="ok", turn_index=-1, sink=memory_sink)


async def test_concurrent_emitters_do_not_lose_or_interleave_records(tmp_path: Path) -> None:
    """Parallel workers share one tracer; every line must still be a complete JSON object."""
    tracer = Tracer.for_turn(session_id="sess-4", turn_index=0, runs_dir=tmp_path)

    async def worker(index: int) -> None:
        await asyncio.sleep(0)
        tracer.blackboard(event="completed", subtask_id=f"s{index}", agent=f"agent{index}")

    await asyncio.gather(*(worker(i) for i in range(50)))
    tracer.close()

    events = read_trace(trace_path(tmp_path, "sess-4", 0))
    assert len(events) == 50
    assert all(isinstance(event, BlackboardEvent) for event in events)
    subtask_ids = {event.subtask_id for event in events if isinstance(event, BlackboardEvent)}
    assert subtask_ids == {f"s{i}" for i in range(50)}


def test_stopwatch_reports_elapsed_time_after_the_block() -> None:
    with stopwatch() as elapsed:
        inside = elapsed()
    after = elapsed()

    assert inside >= 0.0
    assert after >= inside
    assert elapsed() == after  # frozen once the block exits
