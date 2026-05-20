FROM python:3.11-slim

# Build metadata — surface the deployed commit via /health so we can correlate
# a live Railway deploy with a git SHA without grepping the registry.
ARG BUILD_COMMIT=dev
ENV BUILD_COMMIT=${BUILD_COMMIT}

# OCI provenance labels (image-level audit fix). Without these the live
# container can't be traced back to a git commit without consulting the
# registry tag — and Railway re-tags on every deploy, so the tag is lossy.
LABEL org.opencontainers.image.revision="${BUILD_COMMIT}"
LABEL org.opencontainers.image.source="https://github.com/SvenDanilBorodun/Testre"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# VERSION is sourced from the repo root by the railway-deploy workflow
# (which runs `cp ../../VERSION VERSION` before `railway up`). Without it,
# /health.version falls back to GUI_VERSION env (still correct) but the
# main.py drift check between the env var and the in-tree file silently
# no-ops — see CLAUDE.md Rule §6 about the 4-place VERSION agreement.
COPY VERSION ./VERSION

COPY app/ ./app/

EXPOSE 8000

# `--workers 1` is REQUIRED — see CLAUDE.md "Critical architectural choices":
# the in-process rate limiter and dataset reconciliation sweep both assume a
# single uvicorn worker. Raising this without moving the limiter to Redis (or
# a Postgres advisory lock) silently doubles every quota.
#
# `sh -c "exec ..."` so ${PORT} expands at start time (Railway injects it) AND
# uvicorn becomes PID 1 — without `exec`, sh stays as PID 1 and uvicorn never
# receives SIGTERM cleanly on Railway redeploys.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
