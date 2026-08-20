"""Substrate: per-session conversation memory and context compaction.

**Context compaction** is the name for the window-plus-dedup-plus-recap step, in every identifier,
comment and document in this project.  It is not "entropy management": nothing here computes an
entropy, and a label that does not survive a follow-up question is worse than no label.

**Never a process-wide singleton.**  A :class:`WorkingMemory` belongs to one ``session_id`` and is
obtained from a ``MemoryStore`` keyed by it.  A global buffer would mean that two concurrent API
users share a conversation history -- a correctness bug and a privacy one -- and it would make this
module untestable without monkeypatching a module global.

Three things happen to a session's history, and each is a decision with a rejected alternative in
docs/DESIGN.md:

**Window.**  The most recent ``WINDOW_EXCHANGES`` (5) exchanges are replayed in full.

**Recap.**  Everything older is compacted into one message by *deterministic extraction* -- the
question, the opening sentence of the answer, and the documents cited -- never by an LLM call.  An
LLM summarizer would need an ``llm_call.caller`` label the frozen trace schema does not have, its
tokens would be attributed to nothing, and it would put a nondeterministic string into the input of
every later turn, which makes multi-turn evaluation irreproducible.  Extraction produces a recap a
reader can check line by line against the transcript.

**Dedup.**  Tool observations are hashed and counted once per session, so the recap's "already
consulted" list does not repeat a passage that three turns retrieved.  ``blake2b`` and not the
built-in ``hash()``, which is salted per process and would make the same session compact differently
on two machines.

**Tool observations are not replayed.**  What memory carries forward is the answer and the
``doc_id`` values behind it, not the retrieved passages.  Replaying observations would require
replaying the matching assistant tool-call messages -- the whole prior ReAct transcript, every turn,
growing without bound -- and it would quietly void the current turn's tool budget, because the model
could answer from evidence it did not retrieve and is not obliged to cite.  Re-retrieval is cheap;
an unbounded and uncited context is not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from consilium.llm.base import Message
from consilium.skills.base import SkillResult
from consilium.trace import RiskLevel

#: How many exchanges are replayed verbatim.  Older ones are compacted into the recap.
WINDOW_EXCHANGES = 5

#: How much of an answer the recap keeps per compacted exchange.
RECAP_ANSWER_CHARS = 220

#: How much of a question the recap keeps.
RECAP_QUESTION_CHARS = 160

#: Marks the recap as history rather than instruction.  Delivered as a ``user`` message, never as a
#: ``system`` one: system content is instruction, and on providers that lift system messages into a
#: top-level parameter (Anthropic does) a system-role recap would be concatenated into the agent's
#: own rules and read as something the user must be told rather than something they already were.
RECAP_PREFIX = "[earlier in this conversation]"

#: Appended to a replayed assistant message so provenance survives compaction.
SOURCES_PREFIX = "[sources: "


def digest(text: str) -> str:
    """Stable content hash for observation dedup.

    ``blake2b`` rather than the built-in ``hash()``: string hashing is salted per process, so the
    same session would compact differently on two machines and identically only by luck.
    """
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


class Observation(BaseModel):
    """One tool result, reduced to what memory keeps of it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill: str
    content_digest: str
    sources: tuple[str, ...] = ()

    @classmethod
    def of(cls, result: SkillResult) -> Observation:
        return cls(
            skill=result.skill,
            content_digest=digest(result.to_observation()),
            sources=result.sources,
        )


class Exchange(BaseModel):
    """One user turn and its delivered answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    answer: str
    observations: tuple[Observation, ...] = ()
    risk_level: RiskLevel | None = None

    @property
    def sources(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for observation in self.observations:
            for doc_id in observation.sources:
                seen.setdefault(doc_id, None)
        return tuple(seen)


class SessionState(BaseModel):
    """The serializable state of one session.  What a non-local backend stores."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    exchanges: list[Exchange] = Field(default_factory=list)
    #: Content digests seen at least once in this session, in first-seen order.
    seen_digests: list[str] = Field(default_factory=list)
    #: How many observations were dropped as duplicates.  Reported by the memory tests and available
    #: to anyone asking what compaction actually saved.
    duplicates_dropped: int = 0


class WorkingMemory:
    """One session's conversation buffer, with context compaction."""

    def __init__(self, session_id: str, *, window: int = WINDOW_EXCHANGES) -> None:
        if window < 1:
            raise ValueError(f"window must be >= 1; got {window}")
        self.window = window
        self.state = SessionState(session_id=session_id)

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def exchanges(self) -> tuple[Exchange, ...]:
        return tuple(self.state.exchanges)

    @property
    def duplicates_dropped(self) -> int:
        return self.state.duplicates_dropped

    def record(
        self,
        *,
        question: str,
        answer: str,
        tool_results: Sequence[SkillResult] = (),
        risk_level: RiskLevel | None = None,
    ) -> Exchange:
        """Append one exchange, deduplicating its tool observations by content hash."""
        kept: list[Observation] = []
        for result in tool_results:
            if not result.ok:
                continue  # a failed tool call observed nothing worth carrying forward
            observation = Observation.of(result)
            if observation.content_digest in self.state.seen_digests:
                self.state.duplicates_dropped += 1
                continue
            self.state.seen_digests.append(observation.content_digest)
            kept.append(observation)

        exchange = Exchange(
            question=question,
            answer=answer,
            observations=tuple(kept),
            risk_level=risk_level,
        )
        self.state.exchanges.append(exchange)
        return exchange

    def windowed(self) -> tuple[Exchange, ...]:
        """The exchanges replayed verbatim."""
        return tuple(self.state.exchanges[-self.window :])

    def compacted(self) -> tuple[Exchange, ...]:
        """The exchanges that fall outside the window and are represented by the recap."""
        older = self.state.exchanges[: -self.window] if self.window else self.state.exchanges
        return tuple(older)

    def recap(self) -> str | None:
        """A deterministic extractive recap of everything older than the window."""
        older = self.compacted()
        if not older:
            return None

        lines = [f"{RECAP_PREFIX} {len(older)} earlier exchange(s), compacted:"]
        for index, exchange in enumerate(older, start=1):
            question = _clip(exchange.question, RECAP_QUESTION_CHARS)
            answer = _clip(_first_sentence(exchange.answer), RECAP_ANSWER_CHARS)
            lines.append(f"{index}. asked: {question}")
            lines.append(f"   answered: {answer}")
            if exchange.risk_level and exchange.risk_level != "routine":
                lines.append(f"   risk level recorded: {exchange.risk_level}")

        consulted = _unique(doc_id for exchange in older for doc_id in exchange.sources)
        if consulted:
            lines.append(f"Documents already consulted: {', '.join(consulted)}.")
        return "\n".join(lines)

    def history(self) -> list[Message]:
        """The prior-turn messages an agent sees, recap first."""
        messages: list[Message] = []
        recap = self.recap()
        if recap is not None:
            messages.append(Message(role="user", content=recap))

        for exchange in self.windowed():
            messages.append(Message(role="user", content=exchange.question))
            sources = exchange.sources
            suffix = f"\n{SOURCES_PREFIX}{', '.join(sources)}]" if sources else ""
            messages.append(Message(role="assistant", content=exchange.answer + suffix))
        return messages

    def clear(self) -> None:
        self.state = SessionState(session_id=self.session_id)

    def to_json(self) -> str:
        return self.state.model_dump_json()

    @classmethod
    def from_json(cls, payload: str, *, window: int = WINDOW_EXCHANGES) -> WorkingMemory:
        state = SessionState.model_validate_json(payload)
        memory = cls(state.session_id, window=window)
        memory.state = state
        return memory

    def __len__(self) -> int:
        return len(self.state.exchanges)


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    """The opening sentence of an answer, which is where the answer's claim usually is.

    Extractive and crude on purpose.  The alternative -- an LLM writing the recap -- is rejected in
    the module docstring, and a cleverer extractor would be a second thing to test for a recap whose
    whole job is to remind a model what was already discussed.
    """
    collapsed = " ".join(text.split())
    for stop in (". ", "! ", "? "):
        index = collapsed.find(stop)
        if index > 0:
            return collapsed[: index + 1]
    return collapsed


def _unique(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)
