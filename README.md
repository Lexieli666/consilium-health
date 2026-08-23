# consilium-health

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

A multi-agent clinical-information assistant: it routes a health question to specialist agents,
grounds the answer in a retrieval corpus, enforces a safety policy on every output, and records a
structured trace of the whole turn so that every reported number can be recomputed from evidence in
this repository.

**Status: in construction (Phase 9 of 10 complete).** No results have been measured yet. The
results table below stays empty until `eval/run.py` has produced them; the numbers it will hold come
from a committed `eval/results/published/summary.json` and nowhere else.

## Architecture

```
Interface       cli.py · consilium/api/  HTTP and terminal entry points
    │
Router          planner · blackboard · synthesizer
    │
Agents          Consultation · Diagnostic · Research · loop.py (the shared ReAct engine)
    │
Skills          7 atomic, self-describing tools · registry
    │
Substrate       retrieval · memory · safety · llm · trace · config · log
```

It is planner–worker orchestration with a shared blackboard, not a swarm: a central planner
decomposes the question, assigns each subtask to a named worker, the workers run in parallel and
write only their own results, and a synthesizer merges them under a fixed precedence rule. There is
no decentralised local-rule behaviour, so calling it a swarm would be a naming error, not a
shorthand.

## Results

| metric | value |
|---|---|
| everything | not measured |

Nothing is reported here until the evaluation harness in `eval/` has produced it on a live
provider, and the run that produced it is committed under `eval/results/published/`, with the
generating commit, date, model and judge model stated alongside.

## Quickstart

```bash
uv sync --extra embeddings   # sentence-transformers + chromadb; not installed in CI
uv run consilium ingest      # load, chunk, embed and index data/corpus/ into data/chroma/
uv run consilium ask "what does guidance say about starting a statin?"
uv run consilium chat        # multi-turn REPL; one session id, one memory path
uv run uvicorn consilium.api.main:app     # POST /v1/ask, POST /v1/chat (SSE), GET /healthz
```

Then open <http://127.0.0.1:8000/> for the single-file demo page: it holds one session, streams a
turn, and paints the escalation banner before the first character of the answer arrives. It is
served by the API rather than opened from the filesystem so that the page and the endpoint share an
origin and no CORS policy has to be opened for a demo; it loads nothing from the network.

`ask` and `chat` need a provider: set `CONSILIUM_PROVIDER` and the matching key in `.env`, or pass
`--script` to replay a scripted `MockProvider` fixture with no key at all. The HTTP API serves the
same turn the CLI runs and the evaluation harness measures; `GET /v1/sessions/{id}` returns the
shape of a conversation and none of its content (see Limitations).

To bring the whole path up with no model download and no key — the offline pipeline the tests use:

```bash
uv run python -c "import uvicorn; from consilium.api.app import create_app; \
    uvicorn.run(create_app(embedder='hash', store='numpy'))"
```

That checks the wiring, not the answers: with `CONSILIUM_PROVIDER=mock` and no script, the planner
consumes the single placeholder reply and the turn reports that no specialist completed. The
routing, the safety layer, the trace and the SSE ordering are all real in that run; the text is
not, deliberately, because a mock reply that reads like content is how a screenshot of a mock ends
up in a README.

`uv run consilium ingest --embedder hash --store numpy` runs the same pipeline end to end with no
model download and no persistence. That is the path the test suite uses, and it exists because the
offline rule is satisfied by second implementations of the `Embedder` and `VectorStore` protocols
rather than by mocking either library.

Development setup:

```bash
uv sync                      # core dependencies + dev tools; no torch, no model downloads
uv run ruff check .
uv run mypy
uv run pytest                # -m "not network" is the default via addopts
```

`uv sync --extra embeddings` additionally installs `sentence-transformers` and `chromadb`, which
are needed for real retrieval quality numbers and are deliberately absent from CI.

## Limitations

- The corpus is educational summary content written for this project. It is not a clinical
  reference and carries no authority.
- **ICD-10 coverage is deliberately narrow.** Seven of the sixteen conditions in the corpus have a
  note that decides a code; the rest are covered only by their chapter map. Coding questions are
  therefore evaluated across coding *conventions* rather than across conditions, and the system
  should not be expected to select a code for a condition it has no selection note for.
  `docs/CORPUS.md` lists exactly which seven.
- **The HTTP API has no authentication, and `GET /v1/sessions/{id}` is not an authorization
  boundary.** Anyone holding a session id can read that session's metadata, which is why the
  endpoint returns no question text, no answer text, no cited document ids and no risk levels —
  only how many turns a conversation holds and how much of it has been compacted. Server-minted ids
  are 96 bits of hex so that they cannot be guessed. Do not hold a real conversation with a
  deployment of this repository.
- **`POST /v1/chat` streams, but the answer body is not a provider-token stream.** The escalation
  banner is genuinely early — it is decided from the question and is emitted before the turn starts
  — while the body is assembled and then delivered incrementally, so its time to first token is the
  turn's latency. The reason, and what would have to change, is in `docs/DESIGN.md`, "Phase 9".
- No claim is made about clinical validity, accuracy on real patients, or regulatory readiness.
  This is not a medical device and it performs no diagnosis.
- Coverage, latency, cost and quality numbers will be stated with their measurement conditions or
  marked `not measured`.

## Documentation

- `docs/DESIGN.md` — every non-obvious design decision, the alternative rejected, and why.
- `docs/EVALUATION.md` — golden-set construction, metric definitions, judge validation, results.
- `docs/SAFETY.md` — policy model, red-flag provenance, repair behaviour, trace retention.
  Written in Phase 10; the safety layer it will document is built and tested.

## License

MIT (see `LICENSE`, added in Phase 10).
