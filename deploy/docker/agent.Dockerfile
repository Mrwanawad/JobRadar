# Python agent worker: LangGraph agents, Elasticsearch, local embeddings.
# Build context: repo root.
FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    FASTEMBED_CACHE_PATH=/app/models PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra platform --no-install-project
COPY jobradar/ jobradar/
COPY profile.example.yaml README.md ./
RUN uv sync --locked --no-dev --extra platform
# Bake the embedding model into the image: no download at runtime, works offline.
RUN /app/.venv/bin/python -c "from jobradar.agent.search import LocalEmbedder; LocalEmbedder()(['warmup'])"

RUN useradd --uid 10001 --no-create-home agent && chown -R agent /app/models
USER agent
ENTRYPOINT ["/app/.venv/bin/python", "-m", "jobradar", "worker"]
