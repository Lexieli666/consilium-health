"""Interface layer: the HTTP API.

``app``     :func:`create_app`, the composed FastAPI application and its four endpoints.
``models``  the Pydantic request and response models.
``main``    the ASGI entry point, ``consilium.api.main:app``.

Nothing in this package reaches below the turn boundary: a request builds a session id and a turn
index, calls ``consilium.runtime.run_turn``, and renders what comes back.  That is deliberate -- it
is what makes an HTTP answer the same object the CLI prints and the evaluation harness measures.
"""

from consilium.api.app import (
    NO_SUCH_SESSION,
    SESSION_PREFIX,
    STREAM_CHUNK_CHARS,
    ApiState,
    SessionLocks,
    create_app,
    stream_turn,
)
from consilium.api.models import (
    MAX_QUESTION_CHARS,
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

__all__ = [
    "MAX_QUESTION_CHARS",
    "NO_SUCH_SESSION",
    "SESSION_PREFIX",
    "STREAM_CHUNK_CHARS",
    "AnswerResponse",
    "ApiState",
    "AskRequest",
    "EscalationEvent",
    "HealthResponse",
    "RouteInfo",
    "SafetyInfo",
    "SessionLocks",
    "SessionResponse",
    "StreamErrorEvent",
    "TokenEvent",
    "create_app",
    "stream_turn",
]
