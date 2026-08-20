"""Where a session lives, and the isolation between sessions.

The isolation test is the point of the whole module: the reference implementation makes short-term
memory a process-wide singleton, which means two concurrent users share a conversation history. That
is a correctness bug and a privacy one, and it is the reason `MemoryStore` is keyed by `session_id`.
"""

from __future__ import annotations

import asyncio

import pytest

from consilium.memory import (
    KEY_PREFIX,
    DictBackend,
    InMemoryStore,
    MemoryStore,
    MemoryStoreError,
    SerializedStore,
    WorkingMemory,
    validate_session_id,
)


@pytest.fixture(params=["in_memory", "serialized"])
def store(request: pytest.FixtureRequest) -> MemoryStore:
    return InMemoryStore() if request.param == "in_memory" else SerializedStore(DictBackend())


def test_two_sessions_never_see_each_other_s_history(store: MemoryStore) -> None:
    """The whole reason session state is keyed rather than global."""
    alice = store.get("alice")
    alice.record(question="I have high blood pressure", answer="Noted.")
    store.save(alice)

    bob = store.get("bob")
    bob.record(question="what about diet", answer="For which condition?")
    store.save(bob)

    assert len(store.get("alice")) == 1
    assert len(store.get("bob")) == 1
    assert store.get("alice").exchanges[0].question == "I have high blood pressure"
    assert store.get("bob").exchanges[0].question == "what about diet"
    assert "blood pressure" not in str(store.get("bob").history())


async def test_concurrent_sessions_stay_isolated_under_interleaving(store: MemoryStore) -> None:
    """Interleaved turns, the way two API clients would actually arrive."""

    async def _converse(session_id: str, turns: int) -> None:
        for index in range(turns):
            memory = store.get(session_id)
            memory.record(question=f"{session_id} q{index}", answer=f"{session_id} a{index}")
            store.save(memory)
            await asyncio.sleep(0)

    await asyncio.gather(*(_converse(f"user{n}", 5) for n in range(6)))

    for n in range(6):
        memory = store.get(f"user{n}")
        assert len(memory) == 5
        assert all(exchange.question.startswith(f"user{n} ") for exchange in memory.exchanges)


def test_an_unknown_session_starts_empty_rather_than_raising(store: MemoryStore) -> None:
    assert len(store.get("never-seen")) == 0


def test_dropping_a_session_removes_only_that_one(store: MemoryStore) -> None:
    for name in ("alice", "bob"):
        memory = store.get(name)
        memory.record(question="q", answer="a")
        store.save(memory)

    store.drop("alice")

    assert len(store.get("alice")) == 0
    assert len(store.get("bob")) == 1


def test_sessions_are_listable(store: MemoryStore) -> None:
    for name in ("bob", "alice"):
        store.save(store.get(name))
    assert store.sessions() == ["alice", "bob"]


@pytest.mark.parametrize("bad", ["", "../escape", "with space", "a" * 65, "/absolute"])
def test_a_session_id_that_could_escape_a_path_or_a_key_is_refused(
    store: MemoryStore, bad: str
) -> None:
    """The same id becomes a trace directory name, so both places validate it identically."""
    with pytest.raises(MemoryStoreError, match="session_id must match"):
        store.get(bad)


def test_validate_session_id_returns_the_id_it_accepted() -> None:
    assert validate_session_id("cli-abc123") == "cli-abc123"


def test_the_in_memory_store_hands_back_the_same_live_object() -> None:
    """Two agents in one turn share memory by sharing the instance -- what the brief needs."""
    store = InMemoryStore()
    assert store.get("s") is store.get("s")


def test_the_serialized_store_round_trips_through_the_backend() -> None:
    backend = DictBackend()
    store = SerializedStore(backend)

    memory = store.get("s")
    memory.record(question="q", answer="a")
    store.save(memory)

    assert list(backend.data) == [f"{KEY_PREFIX}s"]
    restored = store.get("s")
    assert restored is not memory  # a fresh object, deserialized
    assert restored.exchanges == memory.exchanges


def test_the_serialized_store_reports_unreadable_state_rather_than_guessing() -> None:
    backend = DictBackend()
    backend.set(f"{KEY_PREFIX}s", "{not json")

    with pytest.raises(MemoryStoreError, match="unreadable"):
        SerializedStore(backend).get("s")


def test_the_backend_prefix_namespaces_the_keys() -> None:
    """A shared Redis instance must not collide with whatever else is using it."""
    backend = DictBackend()
    backend.set("someone-else:key", "value")
    store = SerializedStore(backend)
    store.save(WorkingMemory("s"))

    assert store.sessions() == ["s"]


def test_the_redis_backend_is_optional_and_not_imported_at_module_scope() -> None:
    """The project does not depend on redis; it must import cleanly without it."""
    import importlib.util
    import sys

    assert importlib.util.find_spec("redis") is None
    assert "redis" not in sys.modules
    import consilium.memory.store  # noqa: F401 - importing it is the assertion


def test_the_redis_backend_works_against_any_client_shaped_object() -> None:
    """Proves the adapter, without claiming anything about a real Redis server."""
    from consilium.memory import RedisBackend

    class _FakeClient:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        def get(self, key: str) -> str | None:
            return self.data.get(key)

        def set(self, key: str, value: str) -> None:
            self.data[key] = value

        def delete(self, key: str) -> None:
            self.data.pop(key, None)

        def scan_iter(self, match: str) -> list[str]:
            prefix = match.rstrip("*")
            return [key for key in self.data if key.startswith(prefix)]

    store = SerializedStore(RedisBackend("redis://unused", client=_FakeClient()))
    memory = store.get("s")
    memory.record(question="q", answer="a")
    store.save(memory)

    assert store.sessions() == ["s"]
    assert len(store.get("s")) == 1
    store.drop("s")
    assert store.sessions() == []
