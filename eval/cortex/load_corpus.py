"""Load Consilium's corpus into Snowflake as one row per chunk, using the project's own chunker.

The point of reusing ``load_corpus`` and ``chunk_corpus`` from the package is that the comparison
in ``eval/cortex/README.md`` then isolates the retriever: Cortex Search indexes exactly the
documents, chunk boundaries and text that the self-hosted BM25 + dense + RRF retriever indexes.

Connection details come from ``~/.snowflake/connections.toml`` (connection name ``consilium``,
override with ``CONSILIUM_SNOWFLAKE_CONNECTION``); nothing in the repository holds a credential.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from snowflake.snowpark import Session

from consilium.retrieval.chunking import chunk_corpus
from consilium.retrieval.corpus import load_corpus

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = ROOT / "data" / "corpus"
TABLE = "CHUNKS"


def connection_name() -> str:
    """The ``connections.toml`` entry to open: ``consilium`` unless the environment overrides it."""
    return os.environ.get("CONSILIUM_SNOWFLAKE_CONNECTION", "consilium")


def open_session() -> Session:
    session: Session = Session.builder.config("connection_name", connection_name()).create()
    return session


def chunk_frame() -> tuple[pd.DataFrame, int]:
    """One row per chunk, plus the document count, from the same loader and chunker the app uses."""
    documents = load_corpus(CORPUS_DIR)
    chunks = chunk_corpus(documents)
    frame = pd.DataFrame(
        {
            "CHUNK_ID": [c.chunk_id for c in chunks],
            "DOC_ID": [c.doc_id for c in chunks],
            "CHUNK_INDEX": [c.chunk_index for c in chunks],
            "CATEGORY": [str(c.category) for c in chunks],
            "TITLE": [c.title for c in chunks],
            "CHUNK_TEXT": [c.text for c in chunks],
        }
    )
    return frame, len(documents)


def main() -> None:
    frame, n_documents = chunk_frame()
    session = open_session()
    try:
        session.write_pandas(
            frame, TABLE, auto_create_table=True, overwrite=True, quote_identifiers=False
        )
        n_docs_in_table = session.sql("SELECT COUNT(DISTINCT DOC_ID) FROM CHUNKS").collect()[0][0]
        longest = session.sql("SELECT MAX(LENGTH(CHUNK_TEXT)) FROM CHUNKS").collect()[0][0]
    finally:
        session.close()
    print(
        f"loaded {len(frame)} chunks from {n_documents} documents; "
        f"{n_docs_in_table} distinct doc_ids in Snowflake; longest chunk {longest} characters"
    )
    if n_docs_in_table != n_documents:
        raise RuntimeError(
            f"Snowflake holds {n_docs_in_table} distinct doc_ids but the corpus has {n_documents}"
        )


if __name__ == "__main__":
    main()
