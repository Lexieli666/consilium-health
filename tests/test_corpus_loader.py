"""The loader enforces four of the frozen corpus conventions at ingest time.

`tests/test_corpus.py` asserts those conventions hold across the 78 notes that exist today.  This
file asserts the loader *rejects* notes that break them, which is the half that keeps holding after
note 79 is written.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from consilium.retrieval import DISCLAIMER, CorpusError, load_corpus, parse_document

VALID = f"""---
doc_id: condition-example
category: condition
title: "An example note"
source: "general clinical reference"
last_reviewed: 2026-08-17
---

{DISCLAIMER}

## A heading

Body text that says something retrievable about the example condition.
"""


def test_a_well_formed_note_parses() -> None:
    document = parse_document(VALID, doc_id="condition-example")

    assert document.doc_id == "condition-example"
    assert document.category == "condition"
    assert document.title == "An example note"
    assert document.last_reviewed == date(2026, 8, 17)


def test_the_disclaimer_is_stripped_from_the_body() -> None:
    """Required by the loader, then excluded from chunk text.

    Identical in every note, it is a zero-IDF term to BM25 and a constant offset on every dense
    vector: it perturbs every embedding while carrying nothing retrievable.
    """
    document = parse_document(VALID, doc_id="condition-example")

    assert DISCLAIMER not in document.body
    assert document.body.startswith("## A heading")


def test_a_missing_disclaimer_is_an_ingest_error() -> None:
    text = VALID.replace(DISCLAIMER, "> Some other blockquote.")

    with pytest.raises(CorpusError, match="disclaimer"):
        parse_document(text, doc_id="condition-example")


def test_an_altered_disclaimer_is_an_ingest_error() -> None:
    """Byte-identical, not merely present: the loader strips a fixed prefix by length."""
    text = VALID.replace("not medical advice", "not medical  advice")

    with pytest.raises(CorpusError, match="disclaimer"):
        parse_document(text, doc_id="condition-example")


def test_doc_id_must_equal_the_filename_stem() -> None:
    """The golden set labels doc_ids; a stem that disagrees silently invalidates every label."""
    with pytest.raises(CorpusError, match="doc_id is the filename stem"):
        parse_document(VALID, doc_id="condition-something-else")


def test_front_matter_keys_must_be_the_frozen_five() -> None:
    text = VALID.replace("last_reviewed: 2026-08-17", "last_reviewed: 2026-08-17\nauthor: someone")

    with pytest.raises(CorpusError, match="front matter must be exactly"):
        parse_document(text, doc_id="condition-example")


def test_front_matter_key_order_is_part_of_the_contract() -> None:
    text = VALID.replace(
        'category: condition\ntitle: "An example note"',
        'title: "An example note"\ncategory: condition',
    )

    with pytest.raises(CorpusError, match="in that order"):
        parse_document(text, doc_id="condition-example")


def test_an_unknown_category_is_rejected() -> None:
    text = VALID.replace("category: condition", "category: miscellaneous")

    with pytest.raises(CorpusError, match="not one of"):
        parse_document(text, doc_id="condition-example")


def test_missing_front_matter_is_rejected() -> None:
    with pytest.raises(CorpusError, match="front-matter fence"):
        parse_document("just a body\n", doc_id="condition-example")


def test_unclosed_front_matter_is_rejected() -> None:
    with pytest.raises(CorpusError, match="not closed"):
        parse_document("---\ndoc_id: condition-example\n", doc_id="condition-example")


def test_a_note_with_no_body_is_rejected() -> None:
    text = VALID[: VALID.index(DISCLAIMER) + len(DISCLAIMER)] + "\n"

    with pytest.raises(CorpusError, match="no body"):
        parse_document(text, doc_id="condition-example")


def test_a_missing_corpus_directory_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="does not exist"):
        load_corpus(tmp_path / "absent")


def test_an_empty_corpus_directory_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match=r"no \.md notes"):
        load_corpus(tmp_path)


def test_a_non_note_file_in_the_corpus_is_rejected(tmp_path: Path) -> None:
    """Every file is a document, with no exceptions to carve out -- so a README is an error."""
    (tmp_path / "condition-example.md").write_text(VALID, encoding="utf-8")
    (tmp_path / "README.txt").write_text("not a note", encoding="utf-8")

    with pytest.raises(CorpusError, match="ingestable"):
        load_corpus(tmp_path)


def test_a_subdirectory_in_the_corpus_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "condition-example.md").write_text(VALID, encoding="utf-8")
    (tmp_path / "nested").mkdir()

    with pytest.raises(CorpusError, match="ingestable"):
        load_corpus(tmp_path)


def test_documents_are_returned_in_doc_id_order(tmp_path: Path) -> None:
    """Unstable input order makes an index only *nearly* reproducible, which is the worst kind."""
    for stem in ("condition-zebra", "condition-apple", "condition-mango"):
        text = VALID.replace("condition-example", stem)
        (tmp_path / f"{stem}.md").write_text(text, encoding="utf-8")

    assert [document.doc_id for document in load_corpus(tmp_path)] == [
        "condition-apple",
        "condition-mango",
        "condition-zebra",
    ]
