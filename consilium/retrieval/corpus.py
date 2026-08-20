"""Substrate: reading ``data/corpus/`` into documents.

Every file in the corpus directory is an ingestable note, so this loader has no exceptions to carve
out: it globs ``*.md``, and each file it finds is a document whose ``doc_id`` is its filename stem.

The loader is strict on purpose.  Four of the corpus conventions frozen in CLAUDE.md section 7 --
the five front-matter keys in order, ``doc_id`` == stem, the category literal, and the
byte-identical disclaimer -- are *enforced here* rather than only asserted in tests.  A convention
that lives only in a test file is a convention that a note added outside the test run can break;
enforcing it in the loader means a malformed note is an ingest error at the point of ingestion,
which is where someone can act on it.

**The disclaimer is required and then excluded from chunk text.**  It is byte-identical in all 78
notes, so to BM25 it is a zero-IDF term that appears in every document and to a dense embedder it is
a constant offset applied to every vector.  It carries nothing retrievable while perturbing
everything, so it is stripped before the body is chunked.  Requiring it and dropping it are not in
tension: the requirement is a labeling contract, and the exclusion is a retrieval decision.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Final, get_args

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from consilium.retrieval.types import CATEGORIES, Category

#: The five keys, in the frozen order.  Extras are rejected rather than ignored.
FRONT_MATTER_KEYS: Final[tuple[str, ...]] = (
    "doc_id",
    "category",
    "title",
    "source",
    "last_reviewed",
)

#: Byte-identical in every note, immediately after the front matter.  Required by this loader and
#: excluded from chunk text; see the module docstring.
DISCLAIMER: Final = (
    "> **Educational summary — not medical advice.** This note is reference material for a "
    "software\n"
    "> project. It does not diagnose or treat, and it must not be used for real medical decisions."
)

_FRONT_MATTER_FENCE: Final = "---\n"

#: The heading a guideline note uses when authorities genuinely diverge.  A frozen corpus
#: convention (CLAUDE.md section 7), and the hook the ``deep_research`` skill reads its
#: "sources disagree" section out of -- so it is defined once, here with the other conventions, and
#: imported by the skill rather than restated as a literal in two places that could drift apart.
DIFFERS_HEADING: Final = "## Where guidance differs"


class CorpusError(ValueError):
    """Raised when a corpus file violates the format contract.

    A subclass of :class:`ValueError` so that a caller who only wants "this input was bad" does not
    have to import it, and a named class so that the ingest CLI can report the file that failed
    without swallowing unrelated errors.
    """


class Document(BaseModel):
    """One corpus note: validated front matter plus the body, disclaimer removed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    category: Category
    title: str
    source: str
    last_reviewed: date
    body: str


def parse_document(text: str, *, doc_id: str) -> Document:
    """Parse one note's text.  ``doc_id`` is the filename stem and must match the front matter.

    Kept separate from :func:`load_document` so that the format contract can be exercised on
    strings in tests without writing files, and so that a future non-filesystem source (the
    optional MedQuAD loader, for one) reuses the same validation.
    """
    if not text.startswith(_FRONT_MATTER_FENCE):
        raise CorpusError(f"{doc_id}: expected a '---' front-matter fence on the first line")

    closing = text.find(f"\n{_FRONT_MATTER_FENCE}", len(_FRONT_MATTER_FENCE) - 1)
    if closing == -1:
        raise CorpusError(f"{doc_id}: front matter is not closed by a '---' line")

    front_matter_text = text[len(_FRONT_MATTER_FENCE) : closing + 1]
    rest = text[closing + 1 + len(_FRONT_MATTER_FENCE) :]

    front_matter = _parse_front_matter(front_matter_text, doc_id=doc_id)
    body = _strip_disclaimer(rest, doc_id=doc_id)

    if front_matter["doc_id"] != doc_id:
        raise CorpusError(
            f"{doc_id}: front matter says doc_id={front_matter['doc_id']!r}. "
            "doc_id is the filename stem by contract, because the golden set labels it."
        )

    try:
        return Document(**front_matter, body=body)
    except ValidationError as exc:
        raise CorpusError(f"{doc_id}: invalid front matter: {exc}") from exc


def load_document(path: Path) -> Document:
    """Read and validate one corpus file."""
    return parse_document(path.read_text(encoding="utf-8"), doc_id=path.stem)


def load_corpus(directory: Path) -> list[Document]:
    """Read every note in ``directory``, sorted by ``doc_id``.

    Sorted rather than in ``glob`` order so that chunk ordering, and therefore every index built
    from it, is identical on every machine and every filesystem.  Retrieval ties are broken by
    ``chunk_id``, so an unstable input order would produce an index that is only *nearly*
    reproducible -- the hardest kind of irreproducibility to notice.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise CorpusError(f"corpus directory does not exist: {directory}")

    paths = sorted(directory.glob("*.md"))
    if not paths:
        raise CorpusError(f"no .md notes found in {directory}")

    non_notes = sorted(
        entry.name for entry in directory.iterdir() if entry.is_dir() or entry.suffix != ".md"
    )
    if non_notes:
        raise CorpusError(
            f"{directory}: every entry must be an ingestable .md note; found {non_notes}"
        )

    documents = [load_document(path) for path in paths]
    documents.sort(key=lambda document: document.doc_id)
    return documents


def _parse_front_matter(text: str, *, doc_id: str) -> dict[str, Any]:
    """YAML-parse the front matter and assert the five keys, in order, with no extras."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CorpusError(f"{doc_id}: front matter is not valid YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise CorpusError(f"{doc_id}: front matter must be a mapping, got {type(loaded).__name__}")

    keys = tuple(loaded)
    if keys != FRONT_MATTER_KEYS:
        raise CorpusError(
            f"{doc_id}: front matter must be exactly {list(FRONT_MATTER_KEYS)} in that order; "
            f"got {list(keys)}"
        )

    category = loaded["category"]
    if category not in CATEGORIES:
        raise CorpusError(
            f"{doc_id}: category {category!r} is not one of {list(get_args(Category))}"
        )

    return dict(loaded)


def _strip_disclaimer(rest: str, *, doc_id: str) -> str:
    """Require the disclaimer immediately after the front matter, and remove it from the body."""
    remainder = rest.lstrip("\n")
    if not remainder.startswith(DISCLAIMER):
        raise CorpusError(
            f"{doc_id}: the note must open with the byte-identical disclaimer blockquote"
        )
    body = remainder[len(DISCLAIMER) :].strip()
    if not body:
        raise CorpusError(f"{doc_id}: note has no body after the disclaimer")
    return body
