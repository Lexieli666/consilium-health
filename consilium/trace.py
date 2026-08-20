"""Substrate: the turn-scoped trace.

Every number reported in docs/EVALUATION.md and in the README is computed from the JSONL files this
module writes, and from nothing else.  That makes the event schema the load-bearing artifact of the
project: if a metric cannot be derived from the events defined here, the honest response is to say
so rather than to approximate it from a side channel.

One :class:`Tracer` is created per user turn and injected into the planner, router, agents, ReAct
loop, skills, safety layer and synthesizer.  It is deliberately *not* owned by the loop.  The
planner and synthesizer calls are exactly the overhead that the multi-agent architecture adds over
a single LLM call, and leaving them untraced would undercount the cost of the thing the evaluation
exists to measure.

Events are appended to ``runs/<session_id>/<turn_index>.jsonl``, one JSON object per line, each
validated by a versioned Pydantic model.  Traces contain verbatim user questions and full prompts;
docs/SAFETY.md states the retention rule and ``consilium runs purge`` deletes them.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Annotated, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

#: Bumped whenever an event's required fields change.  Stamped on every record so that a trace
#: written by an older commit can be identified rather than silently misread by newer eval code.
#:
#: 2 -- ``turn`` gained ``red_flag_matched_raw`` and ``red_flag_negation_suppressed`` when the
#:      red-flag matcher acquired a negation guard.  Version 1 traces have a ``red_flag_matched``
#:      that means "matched ignoring negation", which is version 2's ``red_flag_matched_raw``; the
#:      two cannot be pooled without that translation, which is the whole reason this number exists.
SCHEMA_VERSION = 2

EventType = Literal["route", "llm_call", "tool_call", "retrieval", "safety", "blackboard", "turn"]

RouteMode = Literal["single", "parallel"]
SafetyKind = Literal["violation", "repair"]
SafetyScope = Literal["tool_call", "output"]
BlackboardKind = Literal["assigned", "started", "completed", "failed", "timeout"]
RiskLevel = Literal["routine", "non-urgent", "urgent", "emergency"]

#: ``planner``, ``synthesizer``, ``forced_answer`` or ``agent:<name>``.  Validated rather than left
#: free-form because ``tokens per turn, split by caller`` is a reported metric and a typo in a
#: caller label would silently create a new bucket.
CALLER_PATTERN = r"^(planner|synthesizer|forced_answer|agent:[a-z][a-z0-9_]*)$"

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TraceError(RuntimeError):
    """Raised when a trace cannot be written or read."""


class _BaseEvent(BaseModel):
    """Fields common to every trace record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    ts: datetime
    trace_id: str
    session_id: str
    turn_index: int = Field(ge=0)
    type: EventType


class PlannedSubtask(BaseModel):
    """One planner assignment, as recorded in a ``route`` event.

    Declared here rather than imported from the router because the trace layer is substrate and may
    not depend on a layer above it.  The router maps its own model onto this one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str
    agent: str
    objective: str
    why: str


class FusedHit(BaseModel):
    """One entry of the fused retrieval ranking: ``(doc_id, chunk_index, rrf_score)``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    chunk_index: int = Field(ge=0)
    rrf_score: float


class RouteEvent(_BaseEvent):
    """How the turn was routed.  Source for routing accuracy and planner fallback rate."""

    type: Literal["route"] = "route"
    mode: RouteMode
    agents: list[str]
    subtasks: list[PlannedSubtask]
    fallback: bool
    latency_ms: float = Field(ge=0)


class LLMCallEvent(_BaseEvent):
    """One provider round trip.  Source for tokens per turn and cost per turn."""

    type: Literal["llm_call"] = "llm_call"
    caller: str = Field(pattern=CALLER_PATTERN)
    provider: str
    model: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    tools_offered: list[str]
    stop_reason: str


class ToolCallEvent(_BaseEvent):
    """One skill execution.  Source for tool calls per turn."""

    type: Literal["tool_call"] = "tool_call"
    agent: str
    skill: str
    args: dict[str, Any]
    ok: bool
    error: str | None = None
    latency_ms: float = Field(ge=0)
    source_doc_ids: list[str] = Field(default_factory=list)


class RetrievalEvent(_BaseEvent):
    """One retrieval.  Source for recall@5, hit@5 and MRR@10.

    ``fused_topk`` carries the full fused top-10 after per-``doc_id`` deduplication even though only
    the first ``returned_k`` entries reach the model.  Truncating the artifact to what the model saw
    is the standard way retrieval metrics become unmeasurable: MRR@10 needs ranks 6-10.
    """

    type: Literal["retrieval"] = "retrieval"
    skill: str
    query: str
    category_filter: str | None = None
    fused_topk: list[FusedHit]
    returned_k: int = Field(ge=0)
    latency_ms: float = Field(ge=0)


class SafetyEvent(_BaseEvent):
    """A policy detection or a repair.

    Violations and repairs are two different counts and are reported as two rates; they are
    distinguished by ``event`` and never merged.  ``post_stream`` marks a repair that was applied
    after tokens had already been delivered to the client, which only happens on the SSE path and
    which docs/SAFETY.md is required to state plainly.
    """

    type: Literal["safety"] = "safety"
    event: SafetyKind
    rule: str
    scope: SafetyScope
    agent: str | None = None
    detail: str
    post_stream: bool = False


class BlackboardEvent(_BaseEvent):
    """A subtask lifecycle transition on the shared blackboard."""

    type: Literal["blackboard"] = "blackboard"
    event: BlackboardKind
    subtask_id: str
    agent: str


class TurnEvent(_BaseEvent):
    """The turn's outcome.  Written once, last.

    The three escalation fields exist because red-flag recall cannot be computed from repair events
    alone.  ``OutputRepair`` prepends the escalation banner only when the answer lacks a seek-care
    instruction, so an answer that already told the user to seek emergency care -- a correct
    handling of the red flag -- emits no repair event at all.  Measuring the repair would score
    that as a false negative.  The detector is therefore run twice, against the model's own answer
    and against the delivered answer:

    ``escalation_present_pre_repair``   the model handled it unaided
    ``escalation_present_post_repair``  the delivered answer escalates -- this is red-flag recall
    ``repair_applied``                  the guard, not the model, is what saved it

    The three ``red_flag_*`` fields record the input-side match under *both* negation policies, so
    that the ablation in docs/EVALUATION.md can report recall and false-positive rate under each
    from a single run rather than from two:

    ``red_flag_matched_raw``            a pattern matched, ignoring negation entirely
    ``red_flag_matched``                a pattern matched and survived the negation guard -- the
                                        policy actually in force, and what drove the banner
    ``red_flag_negation_suppressed``    ``raw and not matched``: the guard changed *this turn's*
                                        outcome.  Exactly the discordant set, so the two policies
                                        can be compared without re-running anything.
    """

    type: Literal["turn"] = "turn"
    question: str
    answer: str
    risk_level: RiskLevel | None = None
    wall_ms: float = Field(ge=0)
    red_flag_matched: bool
    red_flag_matched_raw: bool
    red_flag_negation_suppressed: bool
    escalation_present_pre_repair: bool
    escalation_present_post_repair: bool
    repair_applied: bool


#: The common base of every event.  Exported so that a caller implementing :class:`TraceSink`, or
#: computing metrics over a mixed list, can name the type it receives without reaching for a private
#: name.  It is an alias, not a new model: nothing about the schema changes and ``SCHEMA_VERSION``
#: is unaffected.
TraceEvent = _BaseEvent

type AnyEvent = Annotated[
    RouteEvent
    | LLMCallEvent
    | ToolCallEvent
    | RetrievalEvent
    | SafetyEvent
    | BlackboardEvent
    | TurnEvent,
    Field(discriminator="type"),
]

_EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)

EventT = TypeVar("EventT", bound=_BaseEvent)


class TraceSink(Protocol):
    """Where a :class:`Tracer` puts finished events."""

    def write(self, event: _BaseEvent) -> None: ...

    def close(self) -> None: ...


class JsonlSink:
    """Append events to a single JSONL file, flushing each line.

    Flushing per line costs a syscall per event and buys the property that a trace is readable
    while the turn is still running and survives a crash mid-turn, which matters more: a turn that
    died is exactly the turn whose trace you want.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._handle: Any = None

    def write(self, event: _BaseEvent) -> None:
        line = event.model_dump_json()
        with self._lock:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a", encoding="utf-8")
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


class MemorySink:
    """Collect events in memory.  Used by unit tests and by callers that only want assertions."""

    def __init__(self) -> None:
        self.events: list[_BaseEvent] = []
        self._lock = threading.Lock()

    def write(self, event: _BaseEvent) -> None:
        with self._lock:
            self.events.append(event)

    def close(self) -> None:
        return None

    def of_type(self, event_type: type[EventT]) -> list[EventT]:
        """Return collected events of one class, in write order.

        Keyed on the event class rather than on the ``type`` string so that a caller gets back a
        precisely-typed list and can read ``event.prompt_tokens`` without a cast.
        """
        with self._lock:
            return [event for event in self.events if isinstance(event, event_type)]


class Tracer:
    """Turn-scoped event emitter.

    One instance per user turn, injected into every layer that does work on behalf of that turn.
    All emit methods return the constructed event so a caller (or a test) can assert on it without
    reading the file back.
    """

    def __init__(
        self,
        *,
        session_id: str,
        turn_index: int,
        sink: TraceSink,
        trace_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not _SESSION_ID_PATTERN.match(session_id):
            raise ValueError(
                "session_id must match "
                f"{_SESSION_ID_PATTERN.pattern} (it becomes a directory name); got {session_id!r}"
            )
        if turn_index < 0:
            raise ValueError(f"turn_index must be non-negative; got {turn_index}")

        self.session_id = session_id
        self.turn_index = turn_index
        self.trace_id = trace_id or uuid.uuid4().hex
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def for_turn(
        cls,
        *,
        session_id: str,
        turn_index: int,
        runs_dir: Path,
        trace_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> Tracer:
        """Build a tracer writing to ``<runs_dir>/<session_id>/<turn_index>.jsonl``.

        ``session_id`` is validated against a strict pattern before it reaches the filesystem: it
        arrives from an HTTP request body, and it becomes a directory name.
        """
        if not _SESSION_ID_PATTERN.match(session_id):
            raise ValueError(f"invalid session_id for a trace path: {session_id!r}")
        path = Path(runs_dir) / session_id / f"{turn_index}.jsonl"
        return cls(
            session_id=session_id,
            turn_index=turn_index,
            sink=JsonlSink(path),
            trace_id=trace_id,
            clock=clock,
        )

    def _common(self) -> dict[str, Any]:
        return {
            "ts": self._clock(),
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
        }

    def emit(self, event: _BaseEvent) -> None:
        """Write an already-constructed event.  Prefer the typed helpers below."""
        self._sink.write(event)

    def route(
        self,
        *,
        mode: RouteMode,
        agents: Sequence[str],
        subtasks: Sequence[PlannedSubtask],
        fallback: bool,
        latency_ms: float,
    ) -> RouteEvent:
        event = RouteEvent(
            **self._common(),
            mode=mode,
            agents=list(agents),
            subtasks=list(subtasks),
            fallback=fallback,
            latency_ms=latency_ms,
        )
        self._sink.write(event)
        return event

    def llm_call(
        self,
        *,
        caller: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        tools_offered: Sequence[str],
        stop_reason: str,
    ) -> LLMCallEvent:
        event = LLMCallEvent(
            **self._common(),
            caller=caller,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            tools_offered=list(tools_offered),
            stop_reason=stop_reason,
        )
        self._sink.write(event)
        return event

    def tool_call(
        self,
        *,
        agent: str,
        skill: str,
        args: Mapping[str, Any],
        ok: bool,
        error: str | None,
        latency_ms: float,
        source_doc_ids: Sequence[str] = (),
    ) -> ToolCallEvent:
        event = ToolCallEvent(
            **self._common(),
            agent=agent,
            skill=skill,
            args=dict(args),
            ok=ok,
            error=error,
            latency_ms=latency_ms,
            source_doc_ids=list(source_doc_ids),
        )
        self._sink.write(event)
        return event

    def retrieval(
        self,
        *,
        skill: str,
        query: str,
        category_filter: str | None,
        fused_topk: Sequence[FusedHit],
        returned_k: int,
        latency_ms: float,
    ) -> RetrievalEvent:
        event = RetrievalEvent(
            **self._common(),
            skill=skill,
            query=query,
            category_filter=category_filter,
            fused_topk=list(fused_topk),
            returned_k=returned_k,
            latency_ms=latency_ms,
        )
        self._sink.write(event)
        return event

    def safety(
        self,
        *,
        event: SafetyKind,
        rule: str,
        scope: SafetyScope,
        detail: str,
        agent: str | None = None,
        post_stream: bool = False,
    ) -> SafetyEvent:
        record = SafetyEvent(
            **self._common(),
            event=event,
            rule=rule,
            scope=scope,
            agent=agent,
            detail=detail,
            post_stream=post_stream,
        )
        self._sink.write(record)
        return record

    def blackboard(self, *, event: BlackboardKind, subtask_id: str, agent: str) -> BlackboardEvent:
        record = BlackboardEvent(**self._common(), event=event, subtask_id=subtask_id, agent=agent)
        self._sink.write(record)
        return record

    def turn(
        self,
        *,
        question: str,
        answer: str,
        risk_level: RiskLevel | None,
        wall_ms: float,
        red_flag_matched: bool,
        red_flag_matched_raw: bool,
        red_flag_negation_suppressed: bool,
        escalation_present_pre_repair: bool,
        escalation_present_post_repair: bool,
        repair_applied: bool,
    ) -> TurnEvent:
        event = TurnEvent(
            **self._common(),
            question=question,
            answer=answer,
            risk_level=risk_level,
            wall_ms=wall_ms,
            red_flag_matched=red_flag_matched,
            red_flag_matched_raw=red_flag_matched_raw,
            red_flag_negation_suppressed=red_flag_negation_suppressed,
            escalation_present_pre_repair=escalation_present_pre_repair,
            escalation_present_post_repair=escalation_present_post_repair,
            repair_applied=repair_applied,
        )
        self._sink.write(event)
        return event

    def close(self) -> None:
        self._sink.close()

    def __enter__(self) -> Tracer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@contextmanager
def stopwatch() -> Iterator[Callable[[], float]]:
    """Measure elapsed wall time in milliseconds.

    Every ``latency_ms`` field in the schema is filled from this, so it lives next to the schema.
    The yielded callable reports elapsed time while inside the block and the final duration after
    it, which is what a caller building an event on the way out actually needs.
    """
    start = time.perf_counter()
    end: float | None = None

    def elapsed_ms() -> float:
        stop = time.perf_counter() if end is None else end
        return (stop - start) * 1000.0

    try:
        yield elapsed_ms
    finally:
        end = time.perf_counter()


def parse_event(line: str) -> AnyEvent:
    """Validate one JSONL line against the discriminated union of event models."""
    return _EVENT_ADAPTER.validate_json(line)


def read_trace(path: Path) -> list[AnyEvent]:
    """Read and validate a whole trace file, reporting the line number on the first bad record."""
    events: list[AnyEvent] = []
    with Path(path).open(encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                events.append(parse_event(line))
            except (ValidationError, json.JSONDecodeError) as exc:
                raise TraceError(f"{path}:{lineno}: invalid trace record: {exc}") from exc
    return events


def trace_path(runs_dir: Path, session_id: str, turn_index: int) -> Path:
    """The canonical location of one turn's trace."""
    return Path(runs_dir) / session_id / f"{turn_index}.jsonl"
