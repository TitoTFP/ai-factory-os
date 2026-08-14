# AI Factory OS

> **A control room for teams of AI agents.**
>
> Give a factory a mission. Let specialized agents research, build, review, and report—while humans keep the keys, the budget, and the stop button.

Factory Zero is dogfooding this repository. Factory Zero observed its first verified self-improvement. Factory Zero keeps an auditable trail for every self-improvement. Factory Zero hardened its lease and merge recovery controls.

AI Factory OS is a durable, multi-tenant application for running autonomous agent factories. It combines a FastAPI service, React Factory Floor, PostgreSQL persistence, an OpenAI-compatible provider, encrypted per-factory credentials, scoped tools, lifecycle controls, and an auditable event stream.

## What ships in v0.1

- **Multi-user factories** — email/password sessions, GitHub OAuth, and Google OAuth callback flows.
- **A durable operating model** — `Factory → Space → Agent → Goal/Task → Message/Artifact/Event`.
- **Architect mode** — a real OpenAI-compatible model turns a factory mission into validated spaces, agents, responsibilities, and goals.
- **Autonomous runtime** — start, pause, stop, resume, leased work, stale-worker recovery, retries, escalation, criteria-based evaluation, and usage accounting.
- **Agent collaboration** — typed inbox messages, delegation, review requests, artifact creation, and durable self-reorganization.
- **Permission-derived tools** — workspace files, web fetch, scoped generic HTTP, delegation, review, and reorganization.
- **Factory Floor UI** — onboarding, factory switching, detail drawers, lifecycle controls, live WebSocket activity, and estimated token/cost telemetry.
- **Operational durability** — Alembic migrations, PostgreSQL deployment, SQLite test fallback, named workspace volume, cursor-based events, and idempotent message creation.

This release intentionally excludes distributed workers/Kubernetes, billing, browser automation, named third-party integrations, a plugin marketplace, and native provider SDKs. The provider boundary is OpenAI-compatible HTTP.

## The shape of a factory

```text
                    ┌───────────────────────────┐
                    │       Factory Floor        │
                    │ React UI + live WebSocket  │
                    └─────────────┬─────────────┘
                                  │ HTTP / WS
                    ┌─────────────▼─────────────┐
                    │          FastAPI           │
                    │ auth · runtime · tools     │
                    │ audit · lifecycle · OAuth  │
                    └──────┬─────────┬───────────┘
                           │         │
                 ┌─────────▼───┐ ┌──▼────────────────┐
                 │ PostgreSQL  │ │ OpenAI-compatible │
                 │ state/audit │ │ model endpoint   │
                 └─────────────┘ └───────────────────┘

       Factory
          ├── Space: Research
          │     ├── Agent: Scout
          │     └── Agent: Analyst
          └── Space: Review
                └── Agent: Critic
```

The runtime is intentionally single-host in v0.1. PostgreSQL stores application state and events; the API's named `factory_data` volume stores workspace artifacts outside the container lifecycle.

## Quick start: full stack

### Prerequisites

- Docker Engine with Docker Compose
- A real OpenAI-compatible provider for Architect/runtime calls
- Optional: GitHub or Google OAuth application credentials

### 1. Configure local secrets

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the first value in `SECRET_KEY` and the second in `MASTER_KEY`. Then set `OPENAI_API_KEY` and, if needed, `OPENAI_BASE_URL` and `OPENAI_MODEL` in `.env`.

`MASTER_KEY` encrypts provider credentials at rest. Never commit `.env`, paste its contents into tickets, or expose it in browser code.

### 2. Build and launch

```bash
docker compose up --build -d
```

The Compose stack provides:

| Service | Address | Purpose |
| --- | --- | --- |
| API | <http://localhost:8000> | FastAPI, docs, runtime, WebSocket events |
| API docs | <http://localhost:8000/docs> | Interactive OpenAPI documentation |
| UI | <http://localhost:5173> | React Factory Floor |
| PostgreSQL | `localhost:5433` | Host-mapped database; container port remains `5432` |

Useful checks:

```bash
curl http://localhost:8000/health
docker compose ps
docker compose exec -T db pg_isready -U factory -d factory
docker compose exec -T api alembic current
```

The API container runs migrations before Uvicorn. In production mode it fails closed when `SECRET_KEY` or `MASTER_KEY` is absent or invalid.

## Local development without the API container

Use Compose for PostgreSQL, then run the API and Vite separately:

```bash
cp .env.example .env
# Fill in SECRET_KEY, MASTER_KEY, and provider settings.
docker compose up -d db

.venv/bin/python -m pip install -e .
DATABASE_URL=postgresql+psycopg://factory:factory@localhost:5433/factory \
  PYTHONPATH=backend .venv/bin/alembic upgrade head

DATABASE_URL=postgresql+psycopg://factory:factory@localhost:5433/factory \
  PYTHONPATH=backend .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

For lightweight backend tests, unset `DATABASE_URL`; the test configuration falls back to a local SQLite database. PostgreSQL is the supported deployment database, while SQLite is a practical development/test fallback.

## Configuration

`.env.example` is the complete starting point. The most important settings are:

| Variable | Required | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | deployment | PostgreSQL URL; omit for SQLite fallback in local tests |
| `SECRET_KEY` | yes in production | Session/signing secret; use a random 32+ character value |
| `MASTER_KEY` | yes in production | Fernet key used to encrypt per-factory provider credentials |
| `OPENAI_BASE_URL` | live calls | OpenAI-compatible `/v1` endpoint |
| `OPENAI_MODEL` | live calls | Default model used by Architect/runtime |
| `OPENAI_API_KEY` | live calls | Provider credential used by the smoke/E2E checks |
| `FRONTEND_URL` | recommended | Allowed frontend origin |
| `TASK_LEASE_SECONDS` | optional | Runtime lease duration; defaults to 60 seconds |
| `RUNTIME_POLL_SECONDS` | optional | Runtime polling interval; defaults to 2 seconds |
| `GITHUB_*` | optional | GitHub OAuth client and redirect settings |
| `GOOGLE_*` | optional | Google OAuth client and redirect settings |

A factory can also receive its own OpenAI-compatible base URL, model, and API key during onboarding. Those credentials are encrypted before persistence and are never returned by API responses.

Generate safe development values with:

```bash
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
printf '%s\n%s\n' "$SECRET_KEY" "$MASTER_KEY"
```

## Persistence and workspace boundaries

Compose uses two named volumes:

```text
postgres_data  → PostgreSQL database files
factory_data   → /app/data in the API container
```

Workspace files live at:

```text
/app/data/factories/<factory-id>/agents/<agent-id>/...
/app/data/factories/<factory-id>/agents/_system/...
```

The runtime enforces the following rules:

1. Factory and agent identifiers are single safe path components.
2. `..`, absolute paths, and cross-factory paths are rejected.
3. Agent A cannot read or write Agent B's workspace.
4. Factory, `agents`, and agent scope roots cannot be symlinks.
5. Artifact URIs retain the factory and agent scope that produced them.
6. The Docker persistence test recreates API containers while retaining the named volume.

Do not use `docker compose down -v` when you intend to preserve local data; the `-v` flag deletes named volumes.

## Provider verification

The live smoke test is deliberately explicit and uses the configured external provider rather than a mock:

```bash
OPENAI_API_KEY=... \
OPENAI_BASE_URL=https://api.openai.com/v1 \
OPENAI_MODEL=gpt-4o-mini \
PYTHONPATH=backend .venv/bin/python scripts/live_provider_smoke.py
```

A missing setting exits with code 2. A provider failure or unexpected response exits with code 1. A successful run prints a compact `LIVE_PROVIDER_OK` line.

Run the configured-provider E2E check with:

```bash
OPENAI_API_KEY=... OPENAI_BASE_URL=... OPENAI_MODEL=... \
  PYTHONPATH=backend .venv/bin/python -m pytest -m live tests/e2e/test_live_provider.py -q
```

Usage telemetry estimates token/cost consumption; it is not billing and does not charge accounts.

## OAuth setup

The API starts OAuth with `/api/auth/oauth/start` and validates the returned authorization code server-side before creating a session. Configure the provider credentials and callback URLs in `.env`:

- GitHub: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI`
- Google: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`

For local development, the default callback target is:

```text
http://localhost:8000/api/auth/oauth/callback
```

Register the exact callback URL with the provider application. OAuth state is short-lived and server-validated.

## Security model

AI Factory OS treats model output as untrusted input:

- Tenant-scoped database access prevents one user's factory from being addressed through another user's routes.
- Per-factory provider secrets are encrypted with Fernet and excluded from response schemas.
- Tool schemas are derived from the factory's permissions, so the model does not receive unauthorized operations.
- Workspace paths are normalized and bounded by factory and agent roots.
- Generic HTTP and fetch tools reject private/local destinations, pin DNS decisions, do not follow redirects, and redact sensitive inputs from audit events.
- Credential use, tool calls, messages, task transitions, artifacts, evaluator decisions, and organization changes are audited.
- Leases and startup recovery prevent abandoned work from remaining silently active after a restart.
- Lifecycle controls provide explicit start, pause, stop, and resume boundaries.

This is an application security boundary, not a replacement for host hardening. Run production deployments on a controlled host, protect `.env`, restrict Docker access, and use TLS/reverse-proxy controls at the edge.

## Verification matrix

Run the environment probe first:

```bash
PYTHONPATH=backend .venv/bin/python scripts/verify_environment.py
```

Default deterministic checks:

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
DATABASE_URL=sqlite:///./migration-check.db \
  PYTHONPATH=backend .venv/bin/alembic upgrade head
git diff --check
```

PostgreSQL integration uses a disposable database and an explicit opt-in URL:

```bash
TEST_DATABASE_URL=postgresql+psycopg://factory_test:factory_test@localhost:5432/factory_test \
  PYTHONPATH=backend .venv/bin/python -m pytest -m postgres tests/integration/test_postgres.py -q
```

The Docker persistence/isolation gate uses the Compose API image and named volume:

```bash
RUN_DOCKER_TESTS=1 PYTHONPATH=backend \
  .venv/bin/python -m pytest -m docker tests/integration/test_workspace_persistence.py -q
```

The test suite covers authentication, OAuth callbacks, tenant isolation, Architect validation, criteria evaluation, typed/idempotent messages, tasks, artifacts, events, workspace traversal and symlinks, startup recovery, delegation/review, retry escalation, reorganization, usage accounting, permission-derived tools, tool auditing, Factory Floor lifecycle behavior, WebSocket generation guards, PostgreSQL portability, and container recreation.

Known non-blocking warning: current Starlette/httpx combinations may emit a deprecation warning for `TestClient`; it does not fail the suite.

## Project map

```text
backend/app/              FastAPI routes, models, security, provider, runtime
alembic/                  Database migrations
frontend/src/             React Factory Floor and UI tests
scripts/                  Environment probe and live-provider smoke test
tests/e2e/                 End-to-end runtime/provider/lifecycle checks
tests/integration/        PostgreSQL and container persistence checks
tests/security/           Tenant, credential, SSRF, and boundary checks
Dockerfile.api            API image and migration-first entrypoint
frontend/Dockerfile       Vite development image
docker-compose.yml        PostgreSQL, API, frontend, and named volumes
```

## Operational recipes

```bash
# Follow all service logs
docker compose logs -f

# Follow only the API
docker compose logs -f api

# Restart the API without deleting PostgreSQL or workspace volumes
docker compose up -d --no-deps --force-recreate api

# Stop containers but preserve data
docker compose down

# Inspect named volumes
docker volume ls | grep ai-factory-os

# Destructive reset: deletes PostgreSQL and workspace data
docker compose down -v
```

## License and status

AI Factory OS v0.1 is an actively developed reference application for durable autonomous-agent operations. Review deployment assumptions, provider costs, OAuth configuration, and host security before exposing it beyond a trusted development network.
