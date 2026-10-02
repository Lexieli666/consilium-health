"""Replay Consilium's golden set against a Cortex Search service and report retrieval metrics.

The metric functions are imported from ``eval.metrics``, so recall@5, hit@5 and MRR@10 are computed
by the same code that produced the published self-hosted numbers.  Retrieval depth mirrors the
self-hosted retriever: ask for 20 chunks, deduplicate to documents in rank order, keep the first 5
for recall@5 and hit@5 (``RETURNED_K``) and the first 10 for MRR@10 (``TRACE_DEPTH``).

Usage::

    uv run python -m eval.cortex.run_eval                      # both services, both unfiltered
    uv run python -m eval.cortex.run_eval CONSILIUM_M15 m15    # one service, custom label
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snowflake.core import Root

from eval.cortex.load_corpus import open_session
from eval.items import GoldenItem, load_golden
from eval.metrics import RETURNED_K, TRACE_DEPTH, hit_at_k, percentile, recall_at_k, reciprocal_rank

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "eval" / "data" / "golden.jsonl"
OUT = ROOT / "eval" / "cortex" / "results"
DATABASE = "CONSILIUM"
SCHEMA = "EVAL"
#: Chunks requested per query.  The self-hosted retriever fuses 20 candidates before deduplication.
CHUNK_LIMIT = 20
SERVICES: dict[str, str] = {
    "CONSILIUM_M15": "cortex_m15",
    "CONSILIUM_L20": "cortex_l20",
}


def dedupe_docs(rows: list[dict[str, Any]]) -> list[str]:
    """Document ids in rank order, first occurrence wins -- the shape of ``fused_topk``."""
    seen: set[str] = set()
    docs: list[str] = []
    for row in rows:
        doc_id = str(row["DOC_ID"])
        if doc_id not in seen:
            seen.add(doc_id)
            docs.append(doc_id)
    return docs


def labelled_items() -> list[GoldenItem]:
    """The golden items that carry a relevance label; the published run scores exactly these."""
    return [item for item in load_golden(GOLDEN) if item.relevant_doc_ids]


def score_item(item: GoldenItem, docs: list[str], latency_ms: float) -> dict[str, Any]:
    top5 = docs[:RETURNED_K]
    top10 = docs[:TRACE_DEPTH]
    return {
        "id": item.id,
        "relevant": list(item.relevant_doc_ids),
        "retrieved_top10": top10,
        "recall_at_5": recall_at_k(top5, item.relevant_doc_ids),
        "hit_at_5": hit_at_k(top5, item.relevant_doc_ids),
        "rr_at_10": reciprocal_rank(top10, item.relevant_doc_ids),
        "latency_ms": latency_ms,
    }


def run(service_name: str, *, label: str) -> dict[str, Any]:
    session = open_session()
    try:
        root = Root(session)
        service = root.databases[DATABASE].schemas[SCHEMA].cortex_search_services[service_name]
        items = labelled_items()
        per_item: list[dict[str, Any]] = []
        for item in items:
            started = time.perf_counter()
            response = service.search(
                query=item.question,
                columns=["DOC_ID", "CHUNK_ID", "CATEGORY"],
                limit=CHUNK_LIMIT,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            rows = response.to_dict()["results"]
            per_item.append(score_item(item, dedupe_docs(rows), latency_ms))
    finally:
        session.close()

    latencies = [p["latency_ms"] for p in per_item]
    started_at = datetime.now(UTC)
    summary: dict[str, Any] = {
        "label": label,
        "service": f"{DATABASE}.{SCHEMA}.{service_name}",
        "date": started_at.date().isoformat(),
        "finished_at": started_at.isoformat(timespec="seconds"),
        "n_items": len(per_item),
        "chunk_limit": CHUNK_LIMIT,
        "recall_at_5": statistics.mean(p["recall_at_5"] for p in per_item),
        "hit_at_5": statistics.mean(float(p["hit_at_5"]) for p in per_item),
        "mrr_at_10": statistics.mean(p["rr_at_10"] for p in per_item),
        "latency_ms_p50": percentile(latencies, 0.5),
        "latency_ms_p90": percentile(latencies, 0.9),
        "metric_code": "eval.metrics.recall_at_k / hit_at_k / reciprocal_rank",
    }
    out_dir = OUT / summary["date"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{label}.json").write_text(
        json.dumps({"summary": summary, "items": per_item}, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str]) -> None:
    if len(argv) == 2:
        run(argv[0], label=argv[1])
        return
    if argv:
        raise SystemExit("usage: python -m eval.cortex.run_eval [SERVICE_NAME LABEL]")
    for service_name, label in SERVICES.items():
        run(service_name, label=label)


if __name__ == "__main__":
    main(sys.argv[1:])
