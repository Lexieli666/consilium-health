"""Cross-session recall in SQLite, and the decision to keep it out of every measured run."""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.memory import (
    BRUTE_FORCE_ROW_CEILING,
    Episode,
    EpisodicMemory,
    SqliteEpisodicStore,
)
from consilium.retrieval import HashEmbedder


@pytest.fixture
def store(tmp_path: Path) -> SqliteEpisodicStore:
    return SqliteEpisodicStore(tmp_path / "episodic.db")


@pytest.fixture
def episodic(store: SqliteEpisodicStore) -> EpisodicMemory:
    return EpisodicMemory(store, HashEmbedder(), recall_enabled=True)


def test_the_schema_is_created_on_a_fresh_database(store: SqliteEpisodicStore) -> None:
    assert store.count() == 0


def test_a_session_is_remembered_and_recalled(episodic: EpisodicMemory) -> None:
    episodic.remember(
        session_id="s1",
        question="what should my blood pressure target be",
        key_findings="Guidance differs between authorities on the threshold.",
        risk_level="routine",
        sources=("guideline-hypertension-diagnosis-and-bp-targets",),
    )

    (recalled,) = episodic.recall("blood pressure target")

    assert recalled.episode.session_id == "s1"
    assert recalled.episode.sources == ("guideline-hypertension-diagnosis-and-bp-targets",)
    assert recalled.episode.risk_level == "routine"
    assert 0.0 <= recalled.score <= 1.0


def test_recall_returns_the_nearest_sessions_first(episodic: EpisodicMemory) -> None:
    episodic.remember(session_id="bp", question="blood pressure target", key_findings="targets")
    episodic.remember(session_id="code", question="icd 10 code for asthma", key_findings="J45")

    recalled = episodic.recall("what is the blood pressure target", k=2)

    assert [item.episode.session_id for item in recalled] == ["bp", "code"]


def test_recall_is_capped_at_k(episodic: EpisodicMemory) -> None:
    for index in range(6):
        episodic.remember(session_id=f"s{index}", question=f"q{index}", key_findings="f")

    assert len(episodic.recall("q", k=3)) == 3


def test_one_row_per_session_even_across_many_turns(episodic: EpisodicMemory) -> None:
    """There is no session-end signal in a CLI or a stateless API, so the row is upserted."""
    for turn in range(4):
        episodic.remember(
            session_id="s1", question="the first question", key_findings=f"after turn {turn}"
        )

    assert episodic.store.count() == 1
    (recalled,) = episodic.recall("the first question", k=1)
    assert recalled.episode.key_findings == "after turn 3"


def test_recall_is_off_by_default_and_returns_nothing(store: SqliteEpisodicStore) -> None:
    """Cross-session recall over independent golden items would let item N answer from item N-1."""
    memory = EpisodicMemory(store, HashEmbedder())

    memory.remember(session_id="s1", question="q", key_findings="f")

    assert memory.recall_enabled is False
    assert memory.recall("q") == []
    assert store.count() == 1  # remembering still happens; only recall is disabled


def test_recall_on_an_empty_store_returns_nothing(episodic: EpisodicMemory) -> None:
    assert episodic.recall("anything") == []


def test_a_wrong_sized_embedding_is_refused(store: SqliteEpisodicStore) -> None:
    import numpy as np

    with pytest.raises(ValueError, match="384-dimensional"):
        store.upsert(
            Episode(session_id="s", question="q", key_findings="f"),
            np.zeros(7, dtype=np.float32),
        )


def test_episodes_survive_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "episodic.db"
    first = EpisodicMemory(SqliteEpisodicStore(path), HashEmbedder(), recall_enabled=True)
    first.remember(session_id="s1", question="asthma inhaler", key_findings="stepwise therapy")
    first.close()

    second = EpisodicMemory(SqliteEpisodicStore(path), HashEmbedder(), recall_enabled=True)
    (recalled,) = second.recall("asthma inhaler", k=1)

    assert recalled.episode.key_findings == "stepwise therapy"
    second.close()


def test_clearing_the_store_empties_it(episodic: EpisodicMemory) -> None:
    episodic.remember(session_id="s1", question="q", key_findings="f")
    assert isinstance(episodic.store, SqliteEpisodicStore)
    episodic.store.clear()
    assert episodic.store.count() == 0


def test_the_brute_force_ceiling_is_stated_as_a_constant() -> None:
    """The documented limit and the code's warning threshold must be the same number."""
    assert BRUTE_FORCE_ROW_CEILING == 10_000

    source = Path("consilium/memory/episodic.py").read_text(encoding="utf-8")
    assert "10,000 rows" in source
    assert "sqlite-vec" in source
