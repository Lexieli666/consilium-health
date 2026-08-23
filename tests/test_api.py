"""`POST /v1/ask`, `GET /v1/sessions/{id}` and `GET /healthz`, end to end.

Offline: the runtime is built on the `HashEmbedder`/`NumpyStore` seams with a scripted provider, so
these run with no key, no network and no model download -- the same rule the CLI tests follow.

Driven through `httpx.ASGITransport` rather than through `starlette.testclient.TestClient`. The
test client now asks for a package this project does not depend on, and `httpx` is already the
dependency the brief names; going through the ASGI transport also means the app is exercised as an
ASGI application rather than through a synchronous portal, which is what it will actually be.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from consilium.api import NO_SUCH_SESSION, SESSION_PREFIX, create_app
from consilium.config import Settings, get_preset
from consilium.llm.base import LLMProvider
from consilium.runtime import Runtime
from consilium.trace import SCHEMA_VERSION, read_trace, trace_path
from tests.stubs import TurnProvider

ANSWER = "Blood pressure is measured in millimetres of mercury."


@pytest.fixture
def provider() -> TurnProvider:
    return TurnProvider(answer=ANSWER)


@pytest.fixture
def app(offline_runtime: Callable[[LLMProvider], Runtime], provider: TurnProvider) -> FastAPI:
    return create_app(runtime=offline_runtime(provider))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://consilium.test") as opened:
        yield opened


async def test_healthz_reports_what_the_answers_are_grounded_in(client: httpx.AsyncClient) -> None:
    """A bare "ok" cannot distinguish a server answering from the corpus from one answering from
    nothing, and those are differently trustworthy answers behind the same status code."""
    body = (await client.get("/healthz")).json()

    assert body["status"] == "ok"
    assert body["retrieval"] is True
    assert body["corpus_documents"] > 0
    assert body["config"] == "full"
    assert body["trace_schema_version"] == SCHEMA_VERSION


async def test_healthz_carries_no_credential(client: httpx.AsyncClient) -> None:
    assert "key" not in (await client.get("/healthz")).text.lower()


async def test_an_answer_carries_sources_route_risk_level_and_trace_id(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.post("/v1/ask", json={"question": "what is hypertension"})).json()

    assert ANSWER in body["answer"]
    assert body["route"] == {
        "mode": "single",
        "agents": ["consultation"],
        "fallback": False,
        "missing": [],
    }
    assert body["risk_level"] == "routine"
    assert body["trace_id"]
    assert isinstance(body["sources"], list)


async def test_the_delivered_answer_is_the_repaired_one(client: httpx.AsyncClient) -> None:
    """The API delivers what the safety layer cleared, not what the model wrote: `/v1/ask` does not
    stream, so the repair happens before anything reaches the caller."""
    body = (await client.post("/v1/ask", json={"question": "what is hypertension"})).json()

    assert "Not medical advice" in body["answer"]
    assert "disclaimer" in body["safety"]["repairs"]


async def test_a_session_id_is_minted_when_none_is_given(client: httpx.AsyncClient) -> None:
    body = (await client.post("/v1/ask", json={"question": "what is hypertension"})).json()

    assert body["session_id"].startswith(SESSION_PREFIX)
    assert body["turn_index"] == 0


async def test_turns_of_one_session_see_the_earlier_ones(
    client: httpx.AsyncClient, provider: TurnProvider
) -> None:
    """The second turn's specialist is given the first turn's exchange, because `run_turn` fetches
    the session's `WorkingMemory` by the same session id both times."""
    await client.post("/v1/ask", json={"question": "what is hypertension", "session_id": "s-mem"})
    await client.post("/v1/ask", json={"question": "and what about diet", "session_id": "s-mem"})

    second = [messages for caller, messages in provider.calls if caller == "agent:consultation"][1]
    replayed = " ".join(message.content or "" for message in second)
    assert "what is hypertension" in replayed
    assert ANSWER in replayed


async def test_each_turn_is_written_to_its_own_trace(
    client: httpx.AsyncClient, api_settings: Settings
) -> None:
    for question in ("what is hypertension", "and what about diet"):
        response = await client.post(
            "/v1/ask", json={"question": question, "session_id": "s-trace"}
        )
        assert response.status_code == 200

    for turn_index in (0, 1):
        path = trace_path(api_settings.runs_dir, "s-trace", turn_index)
        events = read_trace(path)
        assert [event for event in events if event.type == "turn"]


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "x" * 5000},
        {"question": "hi", "sessionid": "typo"},
        {"question": "hi", "session_id": "not a valid id!"},
        {},
    ],
)
async def test_a_malformed_request_is_refused(
    client: httpx.AsyncClient, payload: dict[str, str]
) -> None:
    """`extra="forbid"` is what makes the `sessionid` typo a 422 rather than a silently fresh
    conversation on every request, which would read as a memory bug in a layer that works."""
    assert (await client.post("/v1/ask", json=payload)).status_code == 422


async def test_an_unknown_session_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/sessions/never-used")

    assert response.status_code == 404
    assert response.json()["detail"] == NO_SUCH_SESSION


async def test_a_malformed_session_id_is_the_same_404_as_an_unknown_one(
    client: httpx.AsyncClient,
) -> None:
    """One response for every case a stranger could produce. A 422 here would confirm that the
    well-formed ids it did not complain about are the ones worth guessing."""
    malformed = await client.get("/v1/sessions/not!an!id")
    unknown = await client.get("/v1/sessions/never-used")

    assert malformed.status_code == unknown.status_code == 404
    assert malformed.json() == unknown.json()


async def test_reading_a_session_does_not_create_one(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """`MemoryStore.get` creates on miss, so a probe that used it would make every guessed id exist
    -- and would let an unauthenticated caller grow the store one request at a time."""
    await client.get("/v1/sessions/probe-me")

    assert (await client.get("/v1/sessions/probe-me")).status_code == 404
    assert app.state.consilium.runtime.memory.sessions() == []


async def test_a_session_reports_its_shape_and_none_of_its_content(
    client: httpx.AsyncClient,
) -> None:
    question = "what is hypertension"
    await client.post("/v1/ask", json={"question": question, "session_id": "s-shape"})

    response = await client.get("/v1/sessions/s-shape")
    body = response.json()

    assert response.status_code == 200
    assert body == {
        "session_id": "s-shape",
        "turns": 1,
        "window_exchanges": 5,
        "compacted_turns": 0,
        "observations_deduplicated": 0,
    }
    # Not "the fields I expected are absent" but "nothing of the conversation is in the bytes".
    raw = json.dumps(body)
    assert question not in raw
    assert ANSWER not in raw


async def test_a_session_reports_compaction_once_the_window_is_passed(
    client: httpx.AsyncClient,
) -> None:
    for index in range(7):
        await client.post(
            "/v1/ask", json={"question": f"question {index}", "session_id": "s-window"}
        )

    body = (await client.get("/v1/sessions/s-window")).json()

    assert body["turns"] == 7
    assert body["compacted_turns"] == 2


async def test_the_api_refuses_a_runtime_with_memory_off(
    offline_runtime: Callable[[LLMProvider], Runtime], provider: TurnProvider
) -> None:
    """Turn index is the count of recorded exchanges, so a memory-off runtime would write every
    turn of a session into one trace file and a reader could not tell them apart."""
    from dataclasses import replace

    runtime = replace(offline_runtime(provider), config=get_preset("full_no_memory"))

    with pytest.raises(ValueError, match="memory"):
        create_app(runtime=runtime)


async def test_an_explicit_null_session_id_is_the_same_as_omitting_it(
    client: httpx.AsyncClient,
) -> None:
    """A JSON client that sends every field sends `null` for the ones it has no value for."""
    body = (
        await client.post("/v1/ask", json={"question": "what is hypertension", "session_id": None})
    ).json()

    assert body["session_id"].startswith(SESSION_PREFIX)


async def test_the_runtime_is_built_during_startup_from_the_settings(
    api_settings: Settings, provider: TurnProvider
) -> None:
    """The path `uvicorn consilium.api.main:app` takes. Driven through the lifespan directly rather
    than through a test client, because what is being asserted is that startup -- not import --
    is what assembles the runtime."""
    app = create_app(settings=api_settings, provider=provider, embedder="hash", store="numpy")

    assert getattr(app.state, "consilium", None) is None
    async with app.router.lifespan_context(app):
        assert app.state.consilium.runtime.provider is provider
        assert app.state.consilium.runtime.config.name == "full"


async def test_the_openapi_schema_is_generated_without_a_runtime() -> None:
    """`consilium.api.main:app` must be importable by a tool that only wants the schema; building a
    runtime at import time would make that need an embedding model."""
    from consilium.api.main import app as main_app

    schema = main_app.openapi()

    assert set(schema["paths"]) == {"/healthz", "/v1/ask", "/v1/chat", "/v1/sessions/{session_id}"}


async def test_the_runs_directory_is_the_configured_one(
    client: httpx.AsyncClient, api_settings: Settings, tmp_path: Path
) -> None:
    await client.post("/v1/ask", json={"question": "what is hypertension", "session_id": "s-where"})

    assert (api_settings.runs_dir / "s-where" / "0.jsonl").exists()
    assert not (tmp_path / "runs").exists()


async def test_the_demo_page_is_served_from_the_api(client: httpx.AsyncClient) -> None:
    """Same origin as the endpoint it calls, so no CORS policy has to be opened for a demo."""
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Not medical advice" in response.text
    assert "/v1/chat" in response.text


async def test_the_demo_page_loads_nothing_from_the_network(client: httpx.AsyncClient) -> None:
    """A page that fetched a framework from a CDN would put a network request into a project whose
    test rule is that it needs none."""
    page = (await client.get("/")).text

    assert "src=" not in page
    assert "http://" not in page.replace("http://www.w3.org", "")
    assert "https://" not in page


async def test_a_checkout_without_the_demo_page_serves_a_404(
    offline_runtime: Callable[[LLMProvider], Runtime],
    provider: TurnProvider,
    tmp_path: Path,
) -> None:
    """The page is not part of the wheel, so its absence is a 404 and not a startup failure."""
    from dataclasses import replace

    runtime = offline_runtime(provider)
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"root_dir": tmp_path}))
    app = create_app(runtime=runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://consilium.test"
    ) as bare:
        response = await bare.get("/")

    assert response.status_code == 404
    assert "/v1" in response.json()["detail"]
