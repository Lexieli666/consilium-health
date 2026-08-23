"""`POST /v1/chat`: the SSE stream, and the one ordering guarantee it exists to make.

**The escalation banner precedes every token of the answer.**  Escalation is decided on the *input*
-- `data/policy.yaml` supplies the banner and `consilium/safety/escalation.py` decides whether the
answer already carries one -- so it is knowable before generation starts.  A banner that arrived
after two hundred streamed tokens would be a banner the user reads too late, which is the whole
reason the escalation is input-side rather than output-side.

The guarantee is asserted twice, because the two assertions are not the same claim:

* through HTTP, that the `escalation` event appears in the response body before any `token` event
  -- SSE is an ordered byte stream, so that is the order the client sees them in;
* against the generator, that the banner is yielded **before the first provider call** -- which no
  HTTP client can observe, because the ASGI test transport buffers the body.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from consilium.api import ApiState, SessionLocks, create_app, stream_turn
from consilium.config import Settings
from consilium.llm import MockProvider, ScriptedResponse
from consilium.llm.base import LLMProvider
from consilium.runtime import Runtime
from consilium.trace import TurnEvent, read_trace, trace_path
from tests.stubs import TurnProvider

#: A question whose text matches `data/red_flags.yaml`, so the assessment fires on the input alone.
RED_FLAG_QUESTION = "I have crushing chest pain and my left arm hurts"
ROUTINE_QUESTION = "what is hypertension"

#: An answer that does not tell the user to seek care, so the guard has to prepend the banner.
UNESCALATED = "Chest discomfort has many causes, including cardiac and non-cardiac ones."

#: An answer that escalates unaided, so the guard prepends nothing.
ESCALATED = "Call emergency services now. Chest discomfort can be a cardiac emergency."

PLAN = '{"subtasks": [{"agent": "consultation", "objective": "Answer it.", "why": "test"}]}'


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Split an SSE body into `(event name, decoded data)` pairs, in wire order."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in re.split(r"(?:\r?\n){2,}", body.strip()):
        name: str | None = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:") :].strip())
        if name is None and not data:
            continue  # a keep-alive comment
        events.append((name or "message", json.loads("".join(data)) if data else {}))
    return events


def names(events: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def payload(events: Sequence[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    return next(data for event, data in events if event == name)


def tokens(events: Sequence[tuple[str, dict[str, Any]]]) -> str:
    return "".join(data["text"] for event, data in events if event == "token")


@pytest.fixture
def make_app(
    offline_runtime: Callable[[LLMProvider], Runtime],
) -> Callable[[LLMProvider], FastAPI]:
    def build(provider: LLMProvider) -> FastAPI:
        return create_app(runtime=offline_runtime(provider))

    return build


@pytest.fixture
def make_client() -> Callable[[FastAPI], httpx.AsyncClient]:
    def build(app: FastAPI) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://consilium.test"
        )

    return build


@pytest.fixture
async def stream(
    make_app: Callable[[LLMProvider], FastAPI],
    make_client: Callable[[FastAPI], httpx.AsyncClient],
) -> AsyncIterator[Callable[[LLMProvider, str, str | None], Any]]:
    """Run one streamed turn and hand back the parsed events."""
    clients: list[httpx.AsyncClient] = []

    async def run(
        provider: LLMProvider, question: str, session_id: str | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        client = make_client(make_app(provider))
        clients.append(client)
        body: dict[str, str] = {"question": question}
        if session_id is not None:
            body["session_id"] = session_id
        response = await client.post("/v1/chat", json=body)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return parse_sse(response.text)

    yield run
    for client in clients:
        await client.aclose()


async def test_the_escalation_banner_precedes_every_token(stream: Any, policy: Any) -> None:
    events = await stream(TurnProvider(answer=UNESCALATED), RED_FLAG_QUESTION)

    assert names(events)[0] == "escalation"
    assert names(events).index("escalation") < names(events).index("token")
    assert payload(events, "escalation")["banner"] == policy.output.escalation.text
    assert payload(events, "escalation")["risk_level"] == "emergency"


async def test_the_banner_is_yielded_before_the_first_provider_call(
    offline_runtime: Callable[[LLMProvider], Runtime], policy: Any
) -> None:
    """The strict form of the guarantee: not "first in the body" but "before generation started"."""
    provider = TurnProvider(answer=UNESCALATED)
    state = ApiState(runtime=offline_runtime(provider), locks=SessionLocks())

    events = stream_turn(state, RED_FLAG_QUESTION, "s-strict")
    first = await anext(events)

    assert provider.calls == []
    assert first.event == "escalation"
    assert json.loads(str(first.data))["banner"] == policy.output.escalation.text

    async for _ in events:  # drain, so the turn completes and its trace is closed
        pass
    assert provider.calls


async def test_a_routine_question_streams_no_escalation_event(stream: Any) -> None:
    events = await stream(TurnProvider(), ROUTINE_QUESTION)

    assert "escalation" not in names(events)
    assert names(events)[0] == "token"


async def test_the_banner_is_delivered_once_not_twice(stream: Any, policy: Any) -> None:
    """It went out as its own event, so the body it is prepended to has it stripped. `done` still
    carries the complete delivered answer, which is what the `turn` event recorded."""
    events = await stream(TurnProvider(answer=UNESCALATED), RED_FLAG_QUESTION)
    banner = policy.output.escalation.text

    body = tokens(events)
    answer = payload(events, "done")["answer"]

    assert answer.startswith(banner)
    assert banner not in body
    assert answer == f"{banner}\n\n{body}"


async def test_the_tokens_reassemble_the_delivered_answer(stream: Any) -> None:
    events = await stream(TurnProvider(), ROUTINE_QUESTION)

    assert tokens(events) == payload(events, "done")["answer"]
    assert "Not medical advice" in tokens(events)


async def test_the_escalation_event_fires_even_when_the_model_escalated_unaided(
    stream: Any,
) -> None:
    """A correctly-handled red flag emits no repair, and the banner is still the first thing the
    user sees -- because the trigger is the question, not the answer."""
    events = await stream(TurnProvider(answer=ESCALATED), RED_FLAG_QUESTION)

    assert names(events)[0] == "escalation"
    assert "escalation_required" not in payload(events, "done")["safety"]["repairs"]
    assert ESCALATED in tokens(events)


async def test_the_done_event_carries_sources_route_risk_level_and_trace_id(stream: Any) -> None:
    events = await stream(TurnProvider(), ROUTINE_QUESTION)
    done = payload(events, "done")

    assert done["route"]["mode"] == "single"
    assert done["route"]["agents"] == ["consultation"]
    assert done["risk_level"] == "routine"
    assert done["trace_id"]
    assert isinstance(done["sources"], list)
    assert done["turn_index"] == 0


async def test_the_stream_runs_against_the_mock_provider(stream: Any) -> None:
    """The offline rule applies to the SSE path too: a scripted provider, no key, no network."""
    provider = MockProvider(
        [ScriptedResponse(content=PLAN), ScriptedResponse(content="Hypertension is raised BP.")]
    )

    events = await stream(provider, ROUTINE_QUESTION)

    assert "Hypertension is raised BP." in tokens(events)
    assert payload(events, "done")["route"]["fallback"] is False


async def test_the_stream_writes_the_same_trace_a_non_streamed_turn_writes(
    stream: Any, api_settings: Settings
) -> None:
    events = await stream(TurnProvider(), ROUTINE_QUESTION, "s-stream-trace")

    turns = [
        event
        for event in read_trace(trace_path(api_settings.runs_dir, "s-stream-trace", 0))
        if isinstance(event, TurnEvent)
    ]

    assert len(turns) == 1
    assert turns[0].answer == payload(events, "done")["answer"]


async def test_no_repair_on_this_path_is_marked_post_stream(
    stream: Any, api_settings: Settings
) -> None:
    """The answer is repaired before its first byte is delivered, so `safety.post_stream` is never
    set here. docs/DESIGN.md, "Phase 9", states why that is the trade this endpoint makes."""
    await stream(TurnProvider(answer=UNESCALATED), RED_FLAG_QUESTION, "s-post-stream")

    events = read_trace(trace_path(api_settings.runs_dir, "s-post-stream", 0))
    repairs = [event for event in events if event.type == "safety"]

    assert repairs
    assert not any(getattr(event, "post_stream", False) for event in repairs)


async def test_a_failure_after_the_headers_becomes_an_error_event(
    stream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SSE response commits to 200 with its first byte, so a later failure cannot be a status
    code. It is a terminal event, and the client is told the turn failed."""

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the provider fell over")

    monkeypatch.setattr("consilium.api.app.run_turn", explode)

    events = await stream(TurnProvider(), ROUTINE_QUESTION)

    assert names(events) == ["error"]
    assert "the provider fell over" in payload(events, "error")["detail"]
