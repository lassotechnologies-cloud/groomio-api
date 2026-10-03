# groomio-api

FastAPI backend for **Groomio** — appointments, queue, staff, money, messaging and
AI Studio for grooming businesses in Kenya. Multi-tenant, five roles
(`super_admin` · `owner` · `clerk` · `barber` · `customer`).

This repository ships **three** deployable services, because the Celery workers
share the SQLAlchemy models with the API and splitting them would mean a shared
package for no benefit:

| Service | Start command |
|---|---|
| `groomio-api` | `alembic upgrade head && uvicorn app.main:app` |
| `groomio-worker` | `celery -A app.workers.celery_app worker --loglevel=info` |
| `groomio-beat` | `celery -A app.workers.celery_app beat --loglevel=info` |

See [`groomio-infra`](https://github.com/lassotechnologies-cloud/groomio-infra)
for the Railway topology and [`groomio-docs`](https://github.com/lassotechnologies-cloud/groomio-docs)
for the full specification.

## Stack

FastAPI · SQLAlchemy (async) · Alembic · PostgreSQL · Redis · Celery ·
Pydantic v2 · Cloudflare R2 · Africa's Talking (OTP only) · M-Pesa STK Push ·
Pesapal.

## Setup

Requires **Python 3.11**. On macOS with Homebrew the pinned interpreter is
`/usr/local/opt/python@3.11/bin/python3.11`; the system Python 3.12 lacks `pip`
for this toolchain.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # fill in DATABASE_URL and SECRET_KEY at minimum
alembic upgrade head
uvicorn app.main:app --reload
```

For CI and the linter, install `requirements-dev.txt` instead — it pulls in
`requirements.txt` plus `pylint`, which is deliberately kept out of the
production Docker image:

```bash
pip install -r requirements-dev.txt
pylint --disable=all --enable=import-error app tests migrations
```

### Environment variables

[`.env.example`](.env.example) is the authoritative list of variable **names** for
this service. `app.core.config.Settings` sets `env_prefix=""`, so a field named
`r2_account_id` is read from `R2_ACCOUNT_ID` and nothing else.

`tests/test_env_template.py` fails if the template and the settings class drift
apart. `groomio-infra` carries a deployment-wide superset of this template; the
`env-template-drift` CI job checks that one too, so neither can go stale alone.

`SECRET_KEY` and `DATABASE_URL` are required at import time — pydantic raises
during collection if they are missing, before a single test runs.

## Tests

```bash
pytest                       # 233 unit tests, no database needed
```

The unit suite deliberately avoids a database: SQLite would exercise a schema
that does not exist in production, because the models use `JSONB`, `UUID` and
native enums.

### Integration tests (require Postgres)

Sale fan-out, webhook replay and tenancy need a real Postgres. Point
`TEST_DATABASE_URL` at a **throwaway** database:

```bash
createdb groomio_test
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/groomio_test \
  pytest tests/test_sale_fanout_integration.py
```

Without it those tests skip with a reason. The fixture drops and recreates the
schema, so it **refuses to run** against a database whose name does not contain
`test` — that guard is what stops a mistyped URL from wiping live data, and it
fires before any connection is opened.

### The customer-portal contract

`tests/test_frontend_contract.py` pins the OpenAPI schema that
`groomio-customer/lib/api.ts` consumes. It reads only `app.main`, not the
frontend's files, so it runs here regardless of whether that repository is
present — which is what keeps it working now that the two are separate
repositories. It is the thing that catches a `staff_id` renamed to `staffId`:
the frontend would typecheck perfectly and render an empty barber list in
production.

## Migrations

Alembic, sequential, in [`migrations/versions/`](migrations/versions/).

```bash
alembic revision --autogenerate -m "000N_description"
alembic upgrade head
```

[`migrations/check_parity.py`](migrations/check_parity.py) compares the models
against the migration history. Run it before opening a PR that changes a model.

## Deployment

[`railway.toml`](railway.toml) configures the API service. The worker and beat
override only the start command, in the Railway Dashboard — their environment
variables must mirror the API's, because all three import the same `Settings`
and a missing variable crashes the worker on import while the API keeps serving.

## Conventions

- Every business-scoped query filters on `business_id`; every branch-scoped one
  on `branch_id`. One shop never sees another's data.
- Pricing is formula-driven: `price = 40 × days × (1 − discount)`, rounded to the
  nearest KES 10.
- SMS is OTP phone verification only. All customer notifications are in-app +
  push.