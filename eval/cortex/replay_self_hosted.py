"""Replay the golden set against Consilium's own retriever, outside any agent turn.

The published ``single_agent_rag`` recall@5 (0.721) was measured inside agent turns: the model
rewrote each question into a search query, most turns went through a category-filtered skill, and
a few turns never retrieved at all.  The Cortex replay in :mod:`eval.cortex.run_eval` sends the raw
question to an unfiltered index.  Those are different experiments.  This module runs the
self-hosted BM25 + dense + RRF retriever under the Cortex replay's protocol -- raw question, no
category filter, 20 candidates, deduplicated to documents -- so that the two rows in
``eval/cortex/README.md`` differ only in the retriever.

Usage::

    uv run python -m eval.cortex.replay_self_hosted              # bge-small (needs [embeddings])
    uv run python -m eval.cortex.replay_self_hosted --embedder hash
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from typing import Any

from consilium.retrieval.bm25 import Bm25Index
from consilium.retrieval.index import EmbedderName, make_embedder, open_retriever
from consilium.retrieval.store import NumpyStore
from eval.cortex.load_corpus import CORPUS_DIR
from eval.cortex.run_eval import CHUNK_LIMIT, OUT, labelled_items, score_item
from eval.metrics import percentile


def run(embedder_name: EmbedderName, *, label: str) -> dict[str, Any]:
    embedder = make_embedder(embedder_name)
    retriever, report = open_retriever(
        corpus_dir=CORPUS_DIR, embedder=embedder, store=NumpyStore(), lexical=Bm25Index()
    )
    retriever.candidate_depth = CHUNK_LIMIT
    per_item: list[dict[str, Any]] = []
    for item in labelled_items():
        started = time.perf_counter()
        fused = retriever.fuse(item.question)
        latency_ms = (time.perf_counter() - started) * 1000
        docs = [scored.chunk.doc_id for scored in fused]
        per_item.append(score_item(item, docs, latency_ms))

    latencies = [p["latency_ms"] for p in per_item]
    finished = datetime.now(UTC)
    summary: dict[str, Any] = {
        "label": label,
        "service": f"self-hosted HybridRetriever: {report.embedder} + BM25, RRF",
        "date": finished.date().isoformat(),
        "finished_at": finished.isoformat(timespec="seconds"),
        "n_items": len(per_item),
        "chunk_limit": CHUNK_LIMIT,
        "recall_at_5": statistics.mean(p["recall_at_5"] for p in per_item),
        "hit_at_5": statistics.mean(float(p["hit_at_5"]) for p in per_item),
        "mrr_at_10": statistics.mean(p["rr_at_10"] for p in per_item),
        "latency_ms_p50": percentile(latencies, 0.5),
        "latency_ms_p90": percentile(latencies, 0.9),
        "metric_code": "eval.metrics.recall_at_k / hit_at_k / reciprocal_rank",
        "chunks_indexed": report.chunks,
    }
    out_dir = OUT / summary["date"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{label}.json").write_text(
        json.dumps({"summary": summary, "items": per_item}, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--embedder", choices=["bge", "hash"], default="bge")
    args = parser.parse_args(argv)
    embedder_name: EmbedderName = args.embedder
    run(embedder_name, label=f"self_hosted_{embedder_name}")


if __name__ == "__main__":
    main()
