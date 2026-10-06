# syntax=docker/dockerfile:1.7
# Consilium-Health API image: FastAPI service, self-hosted hybrid retriever (BM25 + bge-small +
# RRF), ChromaDB store.  Built for linux/amd64 (ECS Fargate); builds natively on arm64 too.
#
# Two things are deliberate here.  First, torch is installed from PyTorch's CPU wheel index
# rather than from uv.lock: the lock resolves the CUDA build on Linux, which is several GB of
# libraries the service never uses.  Second, the bge-small model is downloaded at build time
# into the image, so a container starts with no network access to Hugging Face and no cold
# download on every deploy.

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    HF_HOME=/opt/hf

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer: only the lock and the project metadata, so it is cached until they change.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-extra embeddings --no-extra cortex

# The service code.
COPY consilium ./consilium
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-extra embeddings --no-extra cortex

# The embeddings extra, with CPU-only torch.  Installed after `uv sync` because sync makes the
# environment match the lock exactly and would otherwise replace this torch with the CUDA one.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python \
        --index-url https://download.pytorch.org/whl/cpu torch \
 && uv pip install --python /app/.venv/bin/python \
        "sentence-transformers>=5,<6" "chromadb>=1.0,<2"

# Bake the embedding model into the image.
RUN /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('BAAI/bge-small-en-v1.5')"


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/Lexieli666/consilium-health" \
      org.opencontainers.image.description="Consilium-Health: multi-agent clinical-information assistant (FastAPI)"

RUN groupadd --gid 1000 consilium && useradd --uid 1000 --gid 1000 --create-home consilium \
 && mkdir -p /app /data && chown consilium:consilium /app /data

WORKDIR /app

COPY --from=builder --chown=consilium:consilium /app/.venv /app/.venv
COPY --from=builder --chown=consilium:consilium /opt/hf /opt/hf
COPY --chown=consilium:consilium consilium ./consilium
COPY --chown=consilium:consilium data/corpus ./data/corpus
COPY --chown=consilium:consilium data/policy.yaml data/red_flags.yaml data/symptom_systems.yaml ./data/
COPY --chown=consilium:consilium web ./web

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    CONSILIUM_PROVIDER=mock \
    CONSILIUM_LOG_FORMAT=json \
    CONSILIUM_CHROMA_DIR=/data/chroma \
    CONSILIUM_EPISODIC_DB=/data/episodic.db \
    CONSILIUM_RUNS_DIR=/data/runs

# /data holds everything the service writes: the ChromaDB index (built from data/corpus on first
# start, reused afterwards), the episodic-memory SQLite file and the per-turn traces.  Mount a
# volume (docker compose) or an EFS access point (ECS) here to persist them across restarts.
VOLUME ["/data"]

USER consilium
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import sys, urllib.request; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "consilium.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
