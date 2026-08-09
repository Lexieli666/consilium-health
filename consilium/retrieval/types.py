"""Substrate: the units retrieval operates on.

``doc_id`` is the corpus filename stem and is the identifier the golden set labels, so it is stable
by contract: renaming a corpus file invalidates every label that points at it.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

#: Every corpus note carries exactly one of these.  Skills pass a category filter so that, for
#: example, ``lookup_disease_code`` never competes with lifestyle prose for a slot in the top-5.
Category = Literal["lifestyle", "coding", "guideline", "condition", "red_flag"]

CATEGORIES: tuple[Category, ...] = get_args(Category)


class Chunk(BaseModel):
    """One retrievable passage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    chunk_index: int = Field(ge=0)
    text: str
    category: Category
    title: str
    source: str

    @property
    def chunk_id(self) -> str:
        """Stable identifier for one chunk of one document."""
        return f"{self.doc_id}#{self.chunk_index}"


class ScoredChunk(BaseModel):
    """A chunk with the score that retrieved it, and which retriever produced that score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk: Chunk
    score: float
    retriever: str
