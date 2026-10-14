# Consilium-Health service image: FastAPI service, self-hosted hybrid retriever (BM25 + bge-small +
# RRF), ChromaDB store.  Built for Cloud Run (linux/amd64); builds natively on arm64 too.
#
# Three things are deliberate here.  First, torch is installed from PyTorch's CPU wheel index
# rather than from uv.lock: the lock resolves the CUDA build on Linux, which is several GB of
# libraries the service never uses.  Second, the bge-small model is downloaded at build time into
# the image, so a container starts with no network access to Hugging Face and no cold download on
# every deploy.  Third, **the ChromaDB index is built into the image at build time** from the
# committed corpus: Cloud Run's filesystem is ephemeral and scales to zero, so an index built on
# first start would be rebuilt on every cold start (~312 chunks through bge on 1 vCPU is tens of
# seconds of request-blocking startup).  `open_retriever` reuses a store whose chunk count matches
# the corpus, so the baked index is picked up as-is.
#
# The eval harness (`eval/`) ships in the image too, so that the golden set can be run as a Cloud
# Run Job against exactly the deployed code, environment and region.  It is not part of the wheel
# and costs nothing at serve time.
#
# No BuildKit-only syntax here -- no `# syntax=` frontend, no `RUN --mount=type=cache`.  Cloud
# Build's `gcr.io/cloud-builders/docker` step, which `gcloud run deploy --source` generates, runs
# the classic engine, and a cache mount is a hard parse error there, not a silently ignored
# optimisation (`the --mount option requires BuildKit`).  The uv cache it bought only sped up
# repeat local builds; the layer cache above still covers the dependency layer.

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    HF_HOME=/opt/hf

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer: only the lock and the project metadata, so it is cached until they change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-extra embeddings --no-extra cortex

# The service code.
COPY consilium ./consilium
RUN uv sync --frozen --no-dev --no-extra embeddings --no-extra cortex

# The embeddings extra, with CPU-only torch.  Installed after `uv sync` because sync makes the
# environment match the lock exactly and would otherwise replace this torch with the CUDA one.
RUN uv pip install --python /app/.venv/bin/python \
        --index-url https://download.pytorch.org/whl/cpu torch \
 && uv pip install --python /app/.venv/bin/python \
        "sentence-transformers>=5,<6" "chromadb>=1.0,<2"

# Bake the embedding model into the image.
RUN /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('BAAI/bge-small-en-v1.5')"

# Bake the ChromaDB index: chunk + embed the committed corpus once, here, so a cold start never
# does it.  Offline from this point: the model is already in /opt/hf.
COPY data ./data
RUN HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ANONYMIZED_TELEMETRY=false \
    CONSILIUM_LOG_FORMAT=console \
    /app/.venv/bin/consilium ingest --corpus-dir /app/data/corpus --chroma-dir /app/data/chroma


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/Lexieli666/consilium-health" \
      org.opencontainers.image.description="Consilium-Health: multi-agent clinical-information assistant (FastAPI)"

RUN groupadd --gid 1000 consilium && useradd --uid 1000 --gid 1000 --create-home consilium \
 && mkdir -p /app /data && chown consilium:consilium /app /data

WORKDIR /app

COPY --from=builder --chown=consilium:consilium /app/.venv /app/.venv
COPY --from=builder --chown=consilium:consilium /opt/hf /opt/hf
COPY --from=builder --chown=consilium:consilium /app/data /app/data
COPY --chown=consilium:consilium consilium ./consilium
COPY --chown=consilium:consilium eval ./eval
COPY --chown=consilium:consilium web ./web

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    ANONYMIZED_TELEMETRY=false \
    CONSILIUM_PROVIDER=mock \
    CONSILIUM_LOG_FORMAT=json \
    CONSILIUM_CHROMA_DIR=/app/data/chroma \
    CONSILIUM_EPISODIC_DB=/data/episodic.db \
    CONSILIUM_RUNS_DIR=/data/runs \
    PORT=8080

# /data holds everything the service writes at run time: the episodic-memory SQLite file and the
# per-turn traces.  On Cloud Run it is the ephemeral in-memory filesystem; mount a volume here to
# keep traces across restarts (docker compose does).  The Chroma index is read from /app/data and
# is never written at run time.

USER consilium
EXPOSE 8080

# Shell form so Cloud Run's $PORT is honoured; 8080 is its default and the EXPOSE above.
CMD ["sh", "-c", "exec uvicorn consilium.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
