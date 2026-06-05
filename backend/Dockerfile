# Omni backend — production-shaped image (no LLM keys baked in).
#
# Build:    docker build -t omni-backend backend/
# Run:      docker run -p 8000:8000 -e OMNI_SKIP_EMBED_BACKFILL=1 omni-backend
# Or with Redis:
#           docker compose up redis    # from repo root
#           docker run -p 8000:8000 \
#             -e OMNI_SESSION_BACKEND=redis \
#             -e REDIS_URL=redis://host.docker.internal:6379/0 \
#             omni-backend
#
# We pin Python 3.11 because the codebase is `from __future__ import annotations`
# clean but `pydantic v2 + sqlite3` are happiest on 3.11+.
FROM python:3.11-slim

# Non-root user — banking app expectations.
RUN groupadd -r omni && useradd -r -g omni omni

# System deps: just curl for the healthcheck. fastembed ONNX comes via pip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies in their own layer so source edits don't bust the wheel cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app
COPY scripts ./scripts

# Bootstrap the demo SQLite at build time so the first request is fast.
# Skips embedding backfill (the ONNX warm happens lazily on first chat).
ENV OMNI_SKIP_EMBED_BACKFILL=1
RUN python -c "from app.main import app; print(f'Routes: {len(app.routes)}')"

EXPOSE 8000

USER omni

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Production uses gunicorn + uvicorn workers; the demo image keeps `--reload`
# off because reload requires write access to the source tree.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
