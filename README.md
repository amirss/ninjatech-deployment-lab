# NinjaTech Deployment Lab

NinjaTech Deployment Lab is an incremental, production-oriented deployment engineering
project. Milestone 2 adds a persistent, idempotent task state machine to the Milestone 1
FastAPI, PostgreSQL, migration, observability, container, and CI foundation.

No task execution, LLM, agent framework, queue, authentication, frontend, or third-party
service integration is included in this milestone.

## Architecture

The application is one stateless ASGI service:

- `GET /health` is a process liveness check and never calls PostgreSQL.
- `GET /ready` executes a bounded `SELECT 1` through SQLAlchemy and returns `503` if the
  database is unavailable.
- Request middleware validates or generates an `X-Request-ID`, returns it to the caller,
  and adds it to JSON request logs.
- Configuration is validated at startup from environment variables.
- Database connections are lazy, allowing the process to start while readiness remains
  false during a database outage.
- Alembic migrations are a separate deployment action, avoiding migration races between
  application replicas.
- Task creation uses a PostgreSQL unique constraint and atomic conflict insert for
  idempotency.
- Task transitions lock one row with `SELECT ... FOR UPDATE` before applying the centralized
  state-machine rules.

## Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Docker with Docker Compose for the container workflow

## Local Python setup

Create a local environment file and change the placeholder password:

```bash
cp .env.example .env
```

When the API runs directly on the host, change the database hostname in
`NINJATECH_DATABASE_URL` from `db` to `localhost`.

Install dependencies, apply migrations, and start the API:

```bash
make install
make migrate
make run
```

The API listens on `http://127.0.0.1:8000` by default.

## Docker Compose

After creating `.env`, start the API and PostgreSQL:

```bash
docker compose up --build
```

Apply the baseline migration in a separate command:

```bash
docker compose run --rm app alembic upgrade head
```

The application deliberately does not wait for PostgreSQL before starting. `/health` can
therefore report the process as alive while `/ready` reports `503` until PostgreSQL accepts
connections.

## Endpoints

### `GET /health`

Returns `200` when the API process can serve requests:

```json
{"status":"ok"}
```

### `GET /ready`

Returns `200` after a successful database connectivity check:

```json
{"status":"ready"}
```

Returns `503` when PostgreSQL is unavailable or the check times out:

```json
{"status":"not_ready"}
```

Both endpoints return an `X-Request-ID` response header. A caller may provide a safe request
ID using the same header; otherwise the service generates one.

### `POST /tasks`

Creates a pending task. `Idempotency-Key` is required and must contain 1–255 visible ASCII
characters:

```bash
curl --request POST http://127.0.0.1:8000/tasks \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: 6eef33e2-6fe4-4d77-ada7-e28326c4e598' \
  --data '{
    "task_type": "code_change",
    "input": {
      "repository": "example/repository",
      "issue_number": 123
    }
  }'
```

A new request returns `201`. Repeating the same key and canonical request returns the
original task with `200` and does not change `updated_at`. Reusing the key for different
request content returns `409`.

The request fingerprint is SHA-256 over validated canonical JSON. Object-key ordering,
including nested ordering, does not matter; array ordering and changed values do matter.
Database uniqueness—not an in-memory check—ensures simultaneous duplicates create one row.

### `GET /tasks/{task_id}`

Returns the current task or `404` when the UUID is not present.

### `POST /tasks/{task_id}/approve`

Changes `pending_approval` to `approved`. Repeating approval is idempotent and preserves
`updated_at`. Approval from any other state returns `409`.

### `POST /tasks/{task_id}/cancel`

Changes `pending_approval` or `approved` to `cancelled`. Repeating cancellation is idempotent
and preserves `updated_at`. Cancellation from `running`, `succeeded`, or `failed` returns
`409`.

Task responses expose the task ID, type, input, status, and timestamps. They never expose the
idempotency key or internal fingerprint.

## Task lifecycle

```text
pending_approval -> approved -> running -> succeeded
        |              |          |
        |              |          -> failed
        |              |
        +--------------+-----------> cancelled
```

Only create, retrieve, approve, and cancel are public in this milestone. Starting, succeeding,
and failing are defined and unit tested as internal state-machine commands.

Concurrent transitions on the same task serialize through a PostgreSQL row lock. For a
pending task, concurrent approve and cancel either produce approve followed by cancel, or
cancel followed by a rejected approve. The committed final state is valid and no update is
lost.

## Configuration

All application variables use the `NINJATECH_` prefix:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `NINJATECH_DATABASE_URL` | Yes | None | SQLAlchemy `postgresql+asyncpg` URL |
| `NINJATECH_APP_NAME` | No | `NinjaTech Deployment Lab` | OpenAPI application name |
| `NINJATECH_ENVIRONMENT` | No | `development` | Runtime environment label |
| `NINJATECH_LOG_LEVEL` | No | `INFO` | Application log threshold |
| `NINJATECH_DB_READY_TIMEOUT_SECONDS` | No | `2.0` | Maximum readiness query duration |

Pydantic validates these values during application startup. Real credentials belong only in
the environment or an ignored `.env` file.

## Logging

Application and request logs are emitted as one JSON object per line to stdout. Request logs
include the request ID, method, path, status code, and duration. Task lifecycle logs include
the task ID, type, previous status, and new status. Query strings, request bodies, complete
task input, raw idempotency keys, authorization headers, and SQL bound parameters are
excluded.

## Quality commands

```bash
make format        # Apply Ruff formatting
make format-check  # Check formatting without changing files
make lint          # Run Ruff lint rules
make typecheck     # Run strict mypy checks
make test          # Run unit and integration-style tests
make check         # Run all non-mutating checks
```

The real PostgreSQL readiness, persistence, constraint, and concurrency tests skip when
`NINJATECH_TEST_DATABASE_URL` is absent. GitHub Actions provides PostgreSQL and always
executes them.

## Continuous integration

GitHub Actions has two independent jobs:

- The quality job checks Ruff formatting and linting, runs strict mypy, verifies an Alembic
  upgrade/downgrade/re-upgrade cycle, and runs all pytest tests including live PostgreSQL
  readiness, task persistence, idempotency, constraint, and concurrency coverage.
- The container smoke job validates the Compose configuration, builds the application image,
  confirms its runtime user is non-root, waits for PostgreSQL, explicitly applies migrations,
  starts the API, and checks `/health` and `/ready`.

The smoke job then stops PostgreSQL while leaving the API running. `/health` must remain
`200` because it reports process liveness, while `/ready` must become `503` because the
service is no longer ready to receive database-dependent traffic. An exit trap removes the
smoke containers, network, and volumes whether the test passes or fails.

Run the same workflow locally when Docker Compose and `curl` are installed:

```bash
make container-smoke
```

The smoke script uses isolated test-only credentials and does not print them. Migrations
remain an explicit command in both the smoke workflow and normal deployment; application
startup never runs Alembic automatically.

## Migrations

Create a new migration after adding SQLAlchemy models:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Review generated migrations before applying them:

```bash
make migrate
```

`0001_baseline` is the no-op foundation revision. `0002_create_tasks` creates the JSONB task
table, global idempotency-key uniqueness, and database status integrity checks without
inserting data.
