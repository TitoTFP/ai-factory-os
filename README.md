# AI Factory OS

AI Factory OS is a durable, multi-tenant workspace where specialized agents share spaces, tasks, messages, artifacts, tools, and an auditable event stream.

## v0.1 scope

- FastAPI + React, PostgreSQL, single-host runtime
- Email/password auth plus callback endpoints for GitHub and Google OAuth
- Factory → Space → Agent → Goal/Task → Message/Artifact/Event primitives
- Factory Architect backed by a real OpenAI-compatible chat endpoint
- Durable start/pause/stop/resume runtime with retries, goal evaluation, and queue-pressure reorganization
- Per-factory encrypted provider credentials and tool permissions
- Workspace file tools, web fetch, and generic HTTP methods
- Factory Floor, onboarding, detail drawers, lifecycle controls, live WebSocket status, activity feed, and estimated token/cost usage

This release intentionally does **not** include distributed workers/Kubernetes, billing, browser automation, named third-party integrations, a plugin marketplace, or native providers other than OpenAI-compatible endpoints.

## Run locally

Prerequisites: Python 3.11+, Node 22+, Docker Compose.

```bash
cp .env.example .env
python -m venv .venv
.venv/bin/pip install -e .
cd frontend && npm ci && cd ..
docker compose up -d db
DATABASE_URL=postgresql+psycopg://factory:factory@localhost:5432/factory .venv/bin/alembic upgrade head
PYTHONPATH=backend DATABASE_URL=postgresql+psycopg://factory:factory@localhost:5432/factory .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# in a second terminal
cd frontend && npm run dev
```

Or run the complete container stack:

```bash
docker compose up --build
```

- API: <http://localhost:8000>
- API docs: <http://localhost:8000/docs>
- UI: <http://localhost:5173>

For local development without PostgreSQL, unset `DATABASE_URL`; the code defaults to a SQLite file. PostgreSQL is the supported v0.1 deployment database.

## Provider setup

The onboarding form stores the factory's OpenAI-compatible base URL, model, and API key. Keys are encrypted with `MASTER_KEY` and never returned by API responses. Default permissions are workspace, web fetch, and generic HTTP. In development, set `SECRET_KEY` and `MASTER_KEY` in `.env`; production fails closed when either is missing or invalid.

The live smoke test intentionally requires explicit configuration and performs a health request plus Architect and agent calls:

```bash
OPENAI_API_KEY=... \
OPENAI_BASE_URL=https://api.openai.com/v1 \
OPENAI_MODEL=gpt-4o-mini \
PYTHONPATH=backend .venv/bin/python scripts/live_provider_smoke.py
```

A missing setting exits with code 2; a provider error or unexpected response exits with code 1. Estimated cost telemetry is usage accounting, not billing.

OAuth uses `/api/auth/oauth/start` to issue a short-lived state and provider authorization URL, then verifies the returned authorization code server-side before creating a session. Configure `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI`.

For production, `ENVIRONMENT=production` requires a 32+ character `SECRET_KEY` and a valid Fernet `MASTER_KEY`; the container runs Alembic migrations before Uvicorn and fails closed when secrets are missing. Generic HTTP tools reject private/local destinations, do not follow redirects, and redact sensitive input from audit events.

## Checks

```bash
.venv/bin/python -m pytest -q
cd frontend && npm test -- --run && npm run build
DATABASE_URL=sqlite:///./migration-check.db PYTHONPATH=backend .venv/bin/alembic upgrade head
git diff --check
```

The backend test suite covers auth, OAuth callback behavior, tenant isolation across resource routes, Architect persistence, criteria evaluation, typed message delivery/idempotency, task/artifact/event execution, workspace traversal, recovery, reorganization, usage accounting, and tool auditing. Frontend tests assert the required surface is present; build/type checking is the primary UI gate. Docker Compose is the supported PostgreSQL deployment path; if Docker is unavailable, run the local SQLite tests and migration check instead.
