"""Two simultaneous sessions must not see each other's history.

There is already a test for this at the memory layer -- `tests/test_memory_store.py` interleaves six
sessions under `asyncio.gather` and asserts none sees another's turns. This one asserts it at the
API layer, because that is where the property would be reintroduced as a bug: a client cached on the
module, a FastAPI dependency memoized with `lru_cache`, or a `WorkingMemory` fetched once at startup
would all leave the memory layer correct and the served conversation wrong.

The assertion is deliberately about what reached the model rather than about what came back. Two
responses can look right while the prompts that produced them were contaminated -- the second
session's answer would simply be *influenced* by the first, which no response-level assertion
catches. So the provider records every call, and the test asserts that **no single LLM call ever saw
two sessions' questions**.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

import httpx
import pytest
from fastapi import FastAPI

from consilium.api import create_app
from consilium.config import Settings
from consilium.llm.base import LLMProvider, Message
from consilium.runtime import Runtime
from consilium.trace import TurnEvent, read_trace, trace_path
from tests.stubs import TurnProvider

#: Questions chosen so each one names a topic no other session mentions, which is what makes
#: "this call saw two sessions" a substring test rather than a bookkeeping exercise.
ALICE = ["what is hypertension", "and what about sodium in the diet"]
BOB = ["what is asthma", "and what about a spacer device"]


@pytest.fixture
def prefix(request: pytest.FixtureRequest) -> str:
    """A session-id prefix unique to the test.

    The runs directory is shared by the whole test session, and a trace sink appends, so two tests
    reusing one session id would write their turns into each other's files -- which looks exactly
    like the leak these tests exist to detect.
    """
    return str(request.node.name).removeprefix("test_")[:40]


@pytest.fixture
def provider() -> TurnProvider:
    return TurnProvider(answer=lambda question: f"An answer about: {question[:60]}")


@pytest.fixture
def app(offline_runtime: Callable[[LLMProvider], Runtime], provider: TurnProvider) -> FastAPI:
    return create_app(runtime=offline_runtime(provider))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://consilium.test") as opened:
        yield opened


async def converse(
    client: httpx.AsyncClient, session_id: str, questions: Sequence[str]
) -> list[dict[str, object]]:
    """Run one session's turns in order, as a real client would."""
    answers: list[dict[str, object]] = []
    for question in questions:
        response = await client.post(
            "/v1/ask", json={"question": question, "session_id": session_id}
        )
        assert response.status_code == 200
        answers.append(response.json())
    return answers


def seen(messages: Sequence[Message]) -> set[str]:
    """Which sessions' questions appear in one call's prompt."""
    text = " ".join(message.content or "" for message in messages)
    return {
        owner
        for owner, questions in (("alice", ALICE), ("bob", BOB))
        for question in questions
        if question in text
    }


async def test_two_sessions_never_appear_in_one_prompt(
    client: httpx.AsyncClient, provider: TurnProvider, prefix: str
) -> None:
    await asyncio.gather(
        converse(client, f"{prefix}-alice", ALICE),
        converse(client, f"{prefix}-bob", BOB),
    )

    assert provider.max_in_flight >= 2, "the two conversations did not actually overlap"
    assert provider.calls
    for caller, messages in provider.calls:
        assert len(seen(messages)) <= 1, f"{caller} was given two sessions' questions"


async def test_each_session_replays_only_its_own_earlier_turn(
    client: httpx.AsyncClient, provider: TurnProvider, prefix: str
) -> None:
    await asyncio.gather(
        converse(client, f"{prefix}-alice", ALICE),
        converse(client, f"{prefix}-bob", BOB),
    )

    specialists = [
        messages for caller, messages in provider.calls if caller == "agent:consultation"
    ]
    second_turns = [messages for messages in specialists if seen(messages)]

    # Each session's second turn replays its own first question, and there are exactly two of them.
    replayed = [sorted(seen(messages)) for messages in second_turns if len(messages) > 2]
    assert ["alice"] in replayed
    assert ["bob"] in replayed


async def test_each_session_reports_its_own_turn_count(
    client: httpx.AsyncClient, prefix: str
) -> None:
    await asyncio.gather(
        converse(client, f"{prefix}-alice", ALICE),
        converse(client, f"{prefix}-bob", BOB),
    )

    for session_id in (f"{prefix}-alice", f"{prefix}-bob"):
        body = (await client.get(f"/v1/sessions/{session_id}")).json()
        assert body["turns"] == 2


async def test_each_session_writes_its_own_traces(
    client: httpx.AsyncClient, api_settings: Settings, prefix: str
) -> None:
    await asyncio.gather(
        converse(client, f"{prefix}-alice", ALICE),
        converse(client, f"{prefix}-bob", BOB),
    )

    for session_id, questions in ((f"{prefix}-alice", ALICE), (f"{prefix}-bob", BOB)):
        for turn_index, question in enumerate(questions):
            events = read_trace(trace_path(api_settings.runs_dir, session_id, turn_index))
            turns = [event for event in events if isinstance(event, TurnEvent)]
            assert [event.question for event in turns] == [question]


async def test_concurrent_requests_on_one_session_are_serialized(
    client: httpx.AsyncClient, api_settings: Settings, prefix: str
) -> None:
    """Two requests on the same session share a buffer and would otherwise derive the same turn
    index, appending two turns' events to one trace file. The lock is per session id, so it
    constrains exactly the pair of requests that share state."""
    responses = await asyncio.gather(
        *(
            client.post(
                "/v1/ask", json={"question": f"question {n}", "session_id": f"{prefix}-same"}
            )
            for n in range(3)
        )
    )

    indices = sorted(response.json()["turn_index"] for response in responses)
    assert indices == [0, 1, 2]
    for turn_index in indices:
        events = read_trace(trace_path(api_settings.runs_dir, f"{prefix}-same", turn_index))
        assert len([event for event in events if isinstance(event, TurnEvent)]) == 1


async def test_two_apps_in_one_process_do_not_share_sessions(
    offline_runtime: Callable[[LLMProvider], Runtime], prefix: str
) -> None:
    """A module-level runtime, or a dependency cached across apps, would make these the same store.
    The state is read off `request.app.state`, so it cannot be."""
    first = create_app(runtime=offline_runtime(TurnProvider()))
    second = create_app(runtime=offline_runtime(TurnProvider()))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first), base_url="http://one.test"
    ) as one:
        await one.post(
            "/v1/ask", json={"question": "what is hypertension", "session_id": f"{prefix}-one"}
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second), base_url="http://two.test"
    ) as two:
        assert (await two.get(f"/v1/sessions/{prefix}-one")).status_code == 404
