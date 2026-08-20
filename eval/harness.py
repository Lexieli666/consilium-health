"""Running one item, and collecting exactly the trace it produced.

The harness is deliberately thin.  It runs a turn through the same ``run_turn`` the CLI and the API
use -- **not** a parallel code path built for measurement -- and hands the resulting events to
``eval/metrics.py``.  A measurement harness that drives the system differently from production is
measuring a system nobody ships.

Each item gets its **own session id**, so that:

* working memory cannot carry item N-1's answer into item N, which would contaminate every metric
  in the direction that flatters the system;
* the trace of each item is a separate file, so a single item can be re-read and checked by hand;
* the multi-turn set, which *does* want a shared session, is the only place a session spans turns.

Events are collected through a ``MemorySink`` **and** written to disk.  The metrics run off the
in-memory copy for speed; the file is what a reviewer opens.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from consilium.runtime import Runtime, TurnOutcome, run_turn
from consilium.trace import JsonlSink, MemorySink, TraceEvent, Tracer, TraceSink, trace_path
from eval.items import GoldenItem, MultiturnConversation

#: Session ids become directory names and cache keys, so an item id has to survive the same
#: validation the tracer applies.  Anything outside the allowed set is folded to a hyphen.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class ItemRun:
    """One golden item, its delivered answer, and the events behind it."""

    item: GoldenItem
    outcome: TurnOutcome | None
    events: list[TraceEvent] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.outcome is not None


@dataclass
class ConversationRun:
    """One multi-turn conversation: one session, one answer and one event list per turn."""

    conversation: MultiturnConversation
    answers: list[str] = field(default_factory=list)
    events_per_turn: list[list[TraceEvent]] = field(default_factory=list)
    error: str | None = None


class _Fanout:
    """Writes every event to two sinks.

    The in-memory copy is what the metrics read; the file is what a reviewer opens.  Recomputing
    metrics by re-reading the files would be the same numbers at more I/O, and keeping only the
    files would mean a crash mid-sweep loses the run's results as well as its evidence.
    """

    def __init__(self, sinks: Sequence[TraceSink]) -> None:
        self._sinks = list(sinks)

    def write(self, event: TraceEvent) -> None:
        for sink in self._sinks:
            sink.write(event)

    def close(self) -> None:
        for sink in self._sinks:
            sink.close()


def session_id_for(item_id: str, *, prefix: str) -> str:
    """A trace-safe session id for one item, namespaced by the configuration that ran it."""
    cleaned = _UNSAFE.sub("-", item_id)[:40].strip("-") or "item"
    return f"{prefix}-{cleaned}"[:64]


async def run_golden_item(
    runtime: Runtime, item: GoldenItem, *, runs_dir: Path, prefix: str
) -> ItemRun:
    """Run one golden item through the production turn path.

    A failure is captured on the record rather than raised: one item that errors must not end a
    150-item sweep that costs money, and an item that failed is a result -- it is counted in the
    denominators and named in the report.
    """
    session_id = session_id_for(item.id, prefix=prefix)
    collected = MemorySink()
    sink = _Fanout([collected, JsonlSink(trace_path(runs_dir, session_id, 0))])
    tracer = Tracer(session_id=session_id, turn_index=0, sink=sink)

    try:
        outcome = await run_turn(runtime, item.question, tracer=tracer)
    except Exception as exc:  # a sweep must survive one bad item
        return ItemRun(item=item, outcome=None, events=list(collected.events), error=str(exc))
    finally:
        tracer.close()

    return ItemRun(item=item, outcome=outcome, events=list(collected.events))


async def run_conversation(
    runtime: Runtime, conversation: MultiturnConversation, *, runs_dir: Path, prefix: str
) -> ConversationRun:
    """Run every turn of a conversation in **one** session, so memory is exercised.

    This is the only place a session spans turns.  Ten of the thirty conversations run seven turns
    or more, which is what takes them past the five-exchange window and into the compaction path;
    thirty two-turn conversations would test the window by never reaching it.
    """
    session_id = session_id_for(conversation.id, prefix=prefix)
    run = ConversationRun(conversation=conversation)

    for turn_index, turn in enumerate(conversation.turns):
        collected = MemorySink()
        sink = _Fanout([collected, JsonlSink(trace_path(runs_dir, session_id, turn_index))])
        tracer = Tracer(session_id=session_id, turn_index=turn_index, sink=sink)
        try:
            outcome = await run_turn(runtime, turn.question, tracer=tracer)
        except Exception as exc:
            run.error = f"turn {turn_index}: {exc}"
            return run
        finally:
            tracer.close()
        run.answers.append(outcome.answer)
        run.events_per_turn.append(list(collected.events))

    return run
