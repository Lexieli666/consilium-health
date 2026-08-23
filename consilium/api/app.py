"""Interface: the HTTP API.

Four endpoints, and one composition rule that decides the shape of all of them: **a request runs
the same ``run_turn`` the CLI and the evaluation harness run.**  The alternative -- a request path
that assembles the layers itself, or a second streaming path through the loop and the router -- is
how an HTTP answer stops being the thing the published numbers were measured on.

``POST /v1/ask``               one question, one JSON answer.
``POST /v1/chat``              the same turn, delivered as Server-Sent Events.
``GET  /v1/sessions/{id}``     structural metadata about a session.  **No conversation content.**
``GET  /healthz``              liveness, plus the facts that decide whether an answer means
                               anything.

**Nothing session-scoped is held here.**  The ``Runtime`` is built once and is read-only; a
session's ``WorkingMemory`` is fetched from ``Runtime.memory`` by ``session_id`` for the duration of
one turn and is never cached on the app, on a module, or in a FastAPI dependency.  That is the
property two concurrent users depend on, and ``tests/test_api_concurrency.py`` asserts it at this
layer rather than only at the memory layer, because this is where a cached dependency or a
module-level client would reintroduce it.

**Turns of one session are serialized; different sessions are not.**  Two requests on the same
session would otherwise race on the same buffer and derive the same turn index, which would
interleave two conversations into one trace file.  The lock is per ``session_id`` and lives on the
app instance, so it constrains exactly the pair of requests that share state and nothing else.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sse_starlette import EventSourceResponse, ServerSentEvent

from consilium.api.models import (
    AnswerResponse,
    AskRequest,
    EscalationEvent,
    HealthResponse,
    RouteInfo,
    SafetyInfo,
    SessionResponse,
    StreamErrorEvent,
    TokenEvent,
)
from consilium.config import RunConfig, Settings, get_preset
from consilium.llm.base import LLMProvider
from consilium.log import bind_turn, clear_turn, get_logger
from consilium.memory.store import MemoryStoreError, validate_session_id
from consilium.retrieval.index import EmbedderName, StoreName
from consilium.runtime import Runtime, TurnOutcome, build_runtime, run_turn
from consilium.safety import violation_rules
from consilium.trace import SCHEMA_VERSION, Tracer

log = get_logger(__name__)

#: How much of the answer body one ``token`` event carries.
#:
#: Characters, not tokens.  The answer is assembled before it is delivered (see docs/DESIGN.md,
#: "Phase 9"), so there are no provider token boundaries left to reproduce, and re-tokenizing the
#: text to invent some would be a fabricated artifact dressed as a measurement.  A client
#: concatenates the ``text`` fields; it must not join them with a separator.
STREAM_CHUNK_CHARS = 32

#: Server-minted session ids.  Opaque and 96 bits wide, because ``GET /v1/sessions/{id}`` has no way
#: to tell the caller who started a session from a caller who guessed its id -- so an id must not be
#: guessable.  See docs/DESIGN.md for what that endpoint returns, and what it deliberately does not.
SESSION_PREFIX = "api-"

#: The response body for a session that cannot be read.  Deliberately the same for an id that never
#: existed, an id that was purged, and a malformed one.
NO_SUCH_SESSION = "no such session"

#: The single-file demo page, relative to ``Settings.root_dir``.  Served by the API itself so that
#: the page and the endpoint share an origin: a demo opened from the filesystem would need a CORS
#: policy, and opening the API to cross-origin requests to make a demo work is a real change to the
#: deployment for a decorative reason.  The file is not part of the wheel, so ``GET /`` is a 404
#: wherever it is absent rather than a startup failure.
DEMO_PAGE = Path("web") / "index.html"


@dataclass
class SessionLocks:
    """One :class:`asyncio.Lock` per session, created on first use.

    ``setdefault`` on a dict is atomic under the interpreter lock and there is no ``await`` between
    the lookup and the insert, so two coroutines racing for the same new session get the same lock
    object.  The dict grows with the number of sessions the process has served; that is acceptable
    for a demo server and is stated in docs/DESIGN.md rather than hidden.
    """

    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def for_session(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())


@dataclass(frozen=True)
class ApiState:
    """Everything a request needs that outlives the request.  Held on ``app.state``."""

    runtime: Runtime
    locks: SessionLocks


def get_state(request: Request) -> ApiState:
    """The app's state, or a 503 if the app was never started.

    Read off the request's app rather than from a module-level variable or a cached dependency.
    Either of those would make two apps in one process -- which is what the test suite builds --
    share a runtime, and a shared runtime is a shared ``MemoryStore``.
    """
    state: ApiState | None = getattr(request.app.state, "consilium", None)
    if state is None:  # pragma: no cover - only reachable if the lifespan never ran
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="runtime is not ready"
        )
    return state


StateDep = Annotated[ApiState, Depends(get_state)]


def create_app(
    *,
    runtime: Runtime | None = None,
    settings: Settings | None = None,
    config: RunConfig | None = None,
    provider: LLMProvider | None = None,
    script: Path | None = None,
    embedder: EmbedderName = "bge",
    store: StoreName = "chroma",
) -> FastAPI:
    """Build the application.

    ``runtime`` is injectable so a test -- or the offline demo -- can supply one built on the
    ``HashEmbedder``/``NumpyStore`` seams and a ``MockProvider``, without the environment and
    without a second wiring path.  When it is omitted the runtime is built during startup rather
    than at import time, so ``consilium.api.main:app`` can be imported by a tool that only wants the
    OpenAPI schema and will not load a corpus to get it.
    """
    if runtime is not None:
        _require_memory(runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built = runtime
        if built is None:
            resolved = settings or Settings.from_env()
            built = build_runtime(
                resolved,
                config=config or get_preset("full"),
                provider=provider,
                script=script,
                embedder=embedder,
                store=store,
            )
            _require_memory(built)
        app.state.consilium = ApiState(runtime=built, locks=SessionLocks())
        log.info(
            "api.ready",
            provider=built.provider.name,
            model=built.provider.model,
            config=built.config.name,
            documents=len(built.documents),
        )
        yield

    app = FastAPI(
        title="consilium-health",
        version="0.1.0",
        summary=(
            "Multi-agent clinical-information assistant. Educational software; not medical advice. "
            "It does not diagnose, treat, or provide clinical guidance."
        ),
        lifespan=lifespan,
    )

    # Set eagerly as well as in the lifespan.  An injected runtime is ready before startup, and a
    # test driving the app through an ASGI transport never runs the lifespan at all; without this
    # every such request would be a 503 that says nothing about the code under test.
    if runtime is not None:
        app.state.consilium = ApiState(runtime=runtime, locks=SessionLocks())

    _register_routes(app)
    return app


def _require_memory(runtime: Runtime) -> None:
    """Refuse a runtime whose working memory is switched off.

    The turn index -- and therefore the trace file a turn is written to -- is the count of exchanges
    the session has already recorded.  With ``RunConfig.memory`` off nothing is ever recorded, so
    every turn of a session would be turn 0 and would append to one file, and a reader could not
    tell one turn from the next.  The memory-off presets exist for the ablation table, which runs
    through the harness; refusing them here is refusing to serve a configuration whose traces would
    be unreadable, rather than a limitation of the API.
    """
    if not runtime.config.memory:
        raise ValueError(
            f"the API needs working memory: run configuration {runtime.config.name!r} has "
            "memory=False, which would write every turn of a session into one trace file"
        )


def _register_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def demo(state: StateDep) -> HTMLResponse:
        """The single-file demo page, when the repository it lives in is what is being served."""
        path = state.runtime.settings.root_dir / DEMO_PAGE
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no demo page at {DEMO_PAGE}; the API itself is under /v1",
            )
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    async def healthz(state: StateDep) -> HealthResponse:
        """Liveness, and what the answers from this process are grounded in."""
        runtime = state.runtime
        return HealthResponse(
            status="ok",
            provider=runtime.provider.name,
            model=runtime.provider.model,
            config=runtime.config.name,
            retrieval=runtime.retriever is not None,
            corpus_documents=len(runtime.documents),
            trace_schema_version=SCHEMA_VERSION,
        )

    @app.post("/v1/ask", response_model=AnswerResponse, tags=["chat"])
    async def ask(request: AskRequest, state: StateDep) -> AnswerResponse:
        """Answer one question and return the delivered answer.

        Not streamed, so the safety layer repairs before anything reaches the caller: there is no
        post-stream case on this path, which is the reason to keep it beside ``/v1/chat``.
        """
        session_id = request.session_id or _new_session_id()
        async with state.locks.for_session(session_id):
            turn_index = _next_turn_index(state.runtime, session_id)
            outcome = await _run(state.runtime, request.question, session_id, turn_index)
        return _answer(outcome, session_id=session_id, turn_index=turn_index)

    @app.post("/v1/chat", tags=["chat"])
    async def chat(request: AskRequest, state: StateDep) -> EventSourceResponse:
        """Answer one question as a Server-Sent Event stream.

        Event types, in the only order they can occur:

        ``escalation``  the input matched the red-flag table.  Emitted **before routing starts**,
                        so it precedes every ``token`` event of the answer.  The banner is decided
                        from the question alone, which is what makes that possible, and it is why
                        the escalation is input-side.  Absent when the input did not match.
        ``token``       one increment of the answer body.  Concatenate them.
        ``done``        the full :class:`AnswerResponse`, including the complete delivered answer.
        ``error``       the turn failed after the response had already committed to 200.
        """
        session_id = request.session_id or _new_session_id()
        return EventSourceResponse(stream_turn(state, request.question, session_id))

    @app.get("/v1/sessions/{session_id}", response_model=SessionResponse, tags=["sessions"])
    async def read_session(session_id: str, state: StateDep) -> SessionResponse:
        """Structural metadata about one session.  Never its questions or its answers.

        **A malformed id, an id that was never used, and an id that was purged all return the same
        404.**  There is no authentication in this project, so the endpoint cannot tell the caller
        who started a session from a caller who guessed its id; one response for every case a
        stranger could produce is what keeps the endpoint from confirming which ids are real.  What
        a legitimate caller loses by that is nothing, because they already hold the id.
        """
        runtime = state.runtime
        try:
            validate_session_id(session_id)
        except MemoryStoreError:
            raise _not_found() from None
        if session_id not in runtime.memory.sessions():
            raise _not_found()

        memory = runtime.memory.get(session_id)
        return SessionResponse(
            session_id=session_id,
            turns=len(memory),
            # Read off the session's own buffer rather than from the module constant: the store
            # decides the window, and a response that reported the default while the store used
            # something else would be a fact about the source code, not about this conversation.
            window_exchanges=memory.window,
            compacted_turns=len(memory.compacted()),
            observations_deduplicated=memory.duplicates_dropped,
        )


async def stream_turn(
    state: ApiState, question: str, session_id: str
) -> AsyncIterator[ServerSentEvent]:
    """The SSE body: banner first, then the answer, then the record of what produced it.

    Public, and not because anything outside this module calls it.  The guarantee that matters here
    is an ordering one -- the banner is yielded *before the first provider call*, not merely before
    the first token in the serialized response -- and an HTTP client cannot observe that: an ASGI
    test transport buffers the body, and a real one interleaves it with the network.  Driving this
    generator directly is what turns the guarantee into an assertion.
    """
    runtime = state.runtime

    # The same table, asked the same question, that `run_turn` will ask again inside the turn.
    # Deterministic and cheap, so the two agree by construction; the alternative -- threading a
    # pre-computed assessment into `run_turn` -- would change the turn boundary's signature for the
    # benefit of one caller, and the turn boundary is what the harness measures.
    assessment = runtime.red_flags.assess(question)
    banner = runtime.policy.output.escalation.text
    if assessment.matched:
        yield _event("escalation", EscalationEvent(banner=banner, risk_level=assessment.urgency))

    async with state.locks.for_session(session_id):
        turn_index = _next_turn_index(runtime, session_id)
        try:
            outcome = await _run(runtime, question, session_id, turn_index)
        except Exception as exc:
            # The response committed to 200 with its first byte, so a failure here cannot be a
            # status code.  It is a terminal event instead, and the client is told the turn failed.
            log.exception("api.stream_failed", session_id=session_id, turn_index=turn_index)
            yield _event("error", StreamErrorEvent(detail=f"{type(exc).__name__}: {exc}"))
            return

    # The banner has already been delivered as its own event, so it is not repeated in the body.
    # `OutputRepair` prepends exactly `banner + "\n\n"`, so the prefix is removed by construction
    # rather than by a heuristic, and `done.answer` still carries the complete delivered text.
    body = _without_banner(outcome.answer, banner)
    for start in range(0, len(body), STREAM_CHUNK_CHARS):
        yield _event("token", TokenEvent(text=body[start : start + STREAM_CHUNK_CHARS]))

    yield _event("done", _answer(outcome, session_id=session_id, turn_index=turn_index))


async def _run(runtime: Runtime, question: str, session_id: str, turn_index: int) -> TurnOutcome:
    """One turn, through the same boundary the CLI and the harness use."""
    with Tracer.for_turn(
        session_id=session_id, turn_index=turn_index, runs_dir=runtime.settings.runs_dir
    ) as tracer:
        bind_turn(session_id=session_id, trace_id=tracer.trace_id, turn_index=turn_index)
        try:
            return await run_turn(runtime, question, tracer=tracer)
        finally:
            clear_turn()


def _next_turn_index(runtime: Runtime, session_id: str) -> int:
    """Which turn of this session is about to run.

    Derived from the session's own recorded exchanges rather than from a counter the API keeps, so
    there is exactly one place that knows how long a conversation is.  Callers hold the session lock
    while they read it, so two concurrent requests on one session cannot both see the same count.
    """
    return len(runtime.memory.get(session_id))


def _answer(outcome: TurnOutcome, *, session_id: str, turn_index: int) -> AnswerResponse:
    return AnswerResponse(
        session_id=session_id,
        turn_index=turn_index,
        answer=outcome.answer,
        sources=outcome.sources,
        route=RouteInfo(
            mode=outcome.mode,
            agents=outcome.agents,
            fallback=outcome.fallback,
            missing=outcome.missing,
        ),
        risk_level=outcome.risk_level,
        trace_id=outcome.trace_id,
        wall_ms=outcome.wall_ms,
        safety=SafetyInfo(
            violations=violation_rules(outcome.safety.violations),
            repairs=outcome.safety.repairs,
        ),
    )


def _event(
    name: str, payload: EscalationEvent | TokenEvent | StreamErrorEvent | AnswerResponse
) -> ServerSentEvent:
    return ServerSentEvent(event=name, data=payload.model_dump_json())


def _without_banner(answer: str, banner: str) -> str:
    """The delivered answer with a prepended escalation banner removed, if there is one."""
    if not answer.startswith(banner):
        return answer
    return answer[len(banner) :].lstrip("\n")


def _new_session_id() -> str:
    return f"{SESSION_PREFIX}{uuid.uuid4().hex[:24]}"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_SUCH_SESSION)
