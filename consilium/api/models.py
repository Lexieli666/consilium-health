"""Interface: the request and response models of the HTTP API.

Every model here is a Pydantic model with ``extra="forbid"``, so an unknown key in a request body
is a 422 rather than a silently ignored field.  That matters more than usual for ``session_id``: a
typo in the key name would otherwise start a fresh conversation on every request, which looks like a
memory bug in a layer that is working correctly.

**Every answer carries ``sources``, ``route``, ``risk_level`` and ``trace_id``.**  Those four are
what make an answer checkable rather than merely plausible -- which documents it was grounded in,
who wrote it, how urgent the input was, and where the evidence for all of that is on disk.  They
are required fields of :class:`AnswerResponse`, so an endpoint cannot forget one.

``GET /v1/sessions/{id}`` deliberately returns none of the above and no conversation content at all;
:class:`SessionResponse` documents what it does return and ``docs/DESIGN.md`` says why.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from consilium.memory.store import MemoryStoreError, validate_session_id
from consilium.trace import RiskLevel, RouteMode

#: Upper bound on a question, enforced at the interface.  The trace records the question verbatim
#: and the planner prompt embeds it, so an unbounded field is an unbounded prompt and an unbounded
#: file.  4,000 characters is far longer than any golden-set item and short enough to reject a paste
#: of a whole document.
MAX_QUESTION_CHARS = 4000


class AskRequest(BaseModel):
    """One question, optionally continuing a session."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    #: Omit it and the server mints an opaque one.  Supply it to continue a conversation; it is
    #: validated against the same pattern the memory store and the tracer use, because it becomes a
    #: cache key in one and a directory name in the other.
    session_id: str | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_session_id(value)
        except MemoryStoreError as exc:
            # Re-raised as ValueError so FastAPI renders it as a 422 field error rather than a 500.
            raise ValueError(str(exc)) from exc


class RouteInfo(BaseModel):
    """Who answered, and whether the plan that put them there was a real plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: RouteMode
    agents: tuple[str, ...]
    #: True when the planner could not produce a usable plan and the single-agent default fired.
    fallback: bool
    #: Specialists whose subtask failed or timed out.  Named in the answer text as well.
    missing: tuple[str, ...] = ()


class SafetyInfo(BaseModel):
    """What the guard found and what it did, as two lists rather than one count.

    Violations and repairs are reported separately here for the same reason they are two rates in
    docs/EVALUATION.md: one says the model produced non-compliant output, the other says the guard
    had to act, and a single number would hide a model getting worse behind a guard that kept
    working.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    violations: tuple[str, ...] = ()
    repairs: tuple[str, ...] = ()


class AnswerResponse(BaseModel):
    """The delivered answer and everything needed to check it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    turn_index: int = Field(ge=0)
    #: The **delivered** answer -- post-repair, byte-identical to what the ``turn`` event recorded.
    answer: str
    sources: tuple[str, ...]
    route: RouteInfo
    risk_level: RiskLevel
    trace_id: str
    wall_ms: float = Field(ge=0)
    safety: SafetyInfo


class EscalationEvent(BaseModel):
    """The first event of a streamed turn whose *input* matched the red-flag table.

    Emitted before routing begins, so it reaches the client before any part of the answer.  The
    banner is decided from the question alone, which is what makes that possible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    banner: str
    risk_level: RiskLevel


class TokenEvent(BaseModel):
    """One increment of the answer body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str


class StreamErrorEvent(BaseModel):
    """A failure that happened after the response headers were already sent.

    An SSE response commits to 200 the moment the first byte leaves, so a later failure cannot be a
    status code.  It is delivered as a terminal event instead, and the client is expected to treat
    it as a failed turn rather than as the end of a successful one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: str


class SessionResponse(BaseModel):
    """Structural metadata about a session.  **No conversation content.**

    This is the only endpoint that hands back anything about a stored conversation, and the project
    has no authentication, so it has no way to tell the caller who started a session from a caller
    who guessed its id.  It therefore returns nothing that would harm a stranger: no question text,
    no answer text, no cited ``doc_id`` values and no risk levels -- all four of which would let the
    holder of an id infer what somebody asked about their health.  Those fields are returned once,
    to the caller who asked the question, in the response to the turn that produced them.

    What is left is the shape of the session: how many turns it holds, how many of them have been
    compacted out of the replay window, and how many duplicate tool observations were dropped.  That
    is enough for a client to show "3 turns in this conversation" and to demonstrate that context
    compaction is running.  See docs/DESIGN.md, "Phase 9".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    #: Exchanges recorded in this session.
    turns: int = Field(ge=0)
    #: How many exchanges are replayed verbatim before compaction starts.
    window_exchanges: int = Field(ge=1)
    #: Exchanges that have fallen outside the window and are represented by the recap.
    compacted_turns: int = Field(ge=0)
    #: Tool observations dropped as duplicates by content hash.
    observations_deduplicated: int = Field(ge=0)


class HealthResponse(BaseModel):
    """Liveness plus the facts that decide whether an answer means anything.

    A health check that only says "ok" cannot distinguish a server that will answer from a corpus
    from one that will answer from nothing, and those produce differently trustworthy answers with
    the same status code.  No credential, and no part of one, appears here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    provider: str
    model: str
    config: str
    retrieval: bool
    corpus_documents: int = Field(ge=0)
    trace_schema_version: int
