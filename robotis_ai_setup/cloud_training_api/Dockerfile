FROM python:3.11-slim

# v2.3.0 follow-up: 2026-05-17 — re-trigger nudge for Railway after
# multiple RAILPACK-fallback snapshot failures on a2048a6 / 51010c7 /
# a2c515f / 923e6c9 / 5262c52 / d3fa2e8. Edits to cloud_training_api/*
# are the only thing that reliably picks the configured DOCKERFILE
# builder; a CLAUDE.md edit is not enough.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
