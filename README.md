# NinjaTech Deployment Lab

NinjaTech Deployment Lab is an incremental, production-oriented deployment engineering
project. Milestone 3 adds reliable worker execution to the persistent, idempotent task
state machine and Milestone 1 application foundation.

PostgreSQL is the only queue and coordination system. No LLM, agent framework, external
integration, authentication, frontend, Redis, Celery, Kafka, SQS, Kubernetes, Terraform,
or AWS infrastructure is included.

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
- A separate single-task worker process claims due tasks with `FOR UPDATE SKIP LOCKED`,
  commits a durable execution attempt, then runs the handler without holding a transaction.
- Short heartbeat and finalization transactions require the current worker, attempt number,
  and lease-token hash. Expired attempts are preserved and reconciled before retry.

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

After creating `.env`, start PostgreSQL, the API, and an idle worker:

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

The worker uses the same non-root application image. It can also be run directly:

```bash
make run-worker
```

No production task handler exists yet. The deterministic `diagnostic` handler is disabled
by default and settings reject enabling it in staging or production. For development or
tests only, set `NINJATECH_ENABLE_DIAGNOSTIC_HANDLER=true`.

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
`available_at` and `updated_at`. The first approval sets `available_at` from PostgreSQL's
clock in the same locked transaction, so the task is immediately eligible for claiming.
Approval from any other state returns `409`.

### `POST /tasks/{task_id}/cancel`

Changes `pending_approval` or `approved` to `cancelled` and returns `200`. For a running
task, it records a durable cancellation request and returns `202`; the owning worker
cooperatively stops the handler and finalizes the task as `cancelled`. Repeating cancellation
is idempotent and preserves `updated_at`. Cancellation from `succeeded` or `failed` returns
`409`.

Task responses additionally expose `attempt_count`, `max_attempts`, `available_at`,
`cancellation_requested_at`, a validated result, and sanitized failure fields. They never
expose an idempotency key, request fingerprint, worker identity, lease token, stack trace,
or database error.

## Task lifecycle

```text
pending_approval -> approved <---- retry with backoff
        |              |                 ^
        |              v                 |
        |           running -> succeeded |
        |              |  \------------->+
        |              +---------------> failed
        +--------------+---------------> cancelled
```

Only create, retrieve, approve, and cancel are public. Claiming, starting, retrying,
succeeding, and failing remain internal worker operations.

Concurrent transitions on the same task serialize through a PostgreSQL row lock. For a
pending task, concurrent approve and cancel either produce approve followed by cancel, or
cancel followed by a rejected approve. The committed final state is valid and no update is
lost.

## Worker execution semantics

Execution is at least once. A claim transaction locks one eligible approved row, changes it
to `running`, assigns an unguessable execution token (only its SHA-256 hash is stored),
increments `attempt_count`, inserts an immutable-numbered `task_attempts` row, and commits
before handler execution. Other workers skip the locked row and can claim different work.

A lease is time-bounded ownership recorded in PostgreSQL; unlike a row lock, it remains
after the claim transaction commits and can expire if a process crashes. Heartbeats extend
the task lease and matching attempt atomically. Every heartbeat, completion, retry, failure,
and cancellation finalization matches the task ID, worker, attempt number, and current
token hash. A stale worker therefore cannot update either record after recovery assigns a
new token.

Recovery uses `FOR UPDATE SKIP LOCKED` on an expired running task. It marks the exact old
attempt `lease_expired`, then atomically cancels a requested task, fails an exhausted task,
or returns it to `approved` with bounded exponential equal-jitter backoff. The claim path is
equivalent to:

```sql
SELECT *
FROM tasks
WHERE status = 'approved'
  AND available_at <= clock_timestamp()
  AND attempt_count < max_attempts
  AND task_type IN (...)
ORDER BY available_at, created_at, id
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

Handlers receive a typed context with cooperative cancellation checkpoints and no direct
database mutation access. Customer cancellation finalizes `cancelled`; handler timeout
retries or fails according to the attempt limit; shutdown grace expiry and ownership loss
perform no finalization and leave recovery to the lease. A handler that suppresses
`CancelledError` cannot turn any of those earlier causes into success.

The final validated result belongs on `tasks` because it is the current public outcome.
Attempt rows retain execution status and sanitized failure evidence without duplicating a
potentially large result.

At-least-once coordination prevents stale database writes but cannot undo an external side
effect performed before a crash. Future external tools must use their own idempotency keys,
bounded calls, outcome verification, and reconciliation; exactly-once execution cannot be
promised across independent systems.

## Configuration

All application variables use the `NINJATECH_` prefix:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `NINJATECH_DATABASE_URL` | Yes | None | SQLAlchemy `postgresql+asyncpg` URL |
| `NINJATECH_APP_NAME` | No | `NinjaTech Deployment Lab` | OpenAPI application name |
| `NINJATECH_ENVIRONMENT` | No | `development` | Runtime environment label |
| `NINJATECH_LOG_LEVEL` | No | `INFO` | Application log threshold |
| `NINJATECH_DB_READY_TIMEOUT_SECONDS` | No | `2.0` | Maximum readiness query duration |
| `NINJATECH_WORKER_POLL_INTERVAL_SECONDS` | No | `1.0` | Idle claim polling interval |
| `NINJATECH_WORKER_LEASE_DURATION_SECONDS` | No | `30.0` | Execution lease duration |
| `NINJATECH_WORKER_HEARTBEAT_INTERVAL_SECONDS` | No | `10.0` | Lease heartbeat interval |
| `NINJATECH_WORKER_HANDLER_TIMEOUT_SECONDS` | No | `300.0` | Per-attempt handler timeout |
| `NINJATECH_WORKER_SHUTDOWN_GRACE_SECONDS` | No | `20.0` | Grace before abandoning active work |
| `NINJATECH_WORKER_DEFAULT_MAX_ATTEMPTS` | No | `3` | Attempts assigned at task creation |
| `NINJATECH_WORKER_RETRY_BASE_SECONDS` | No | `2.0` | Initial retry backoff ceiling |
| `NINJATECH_WORKER_RETRY_CAP_SECONDS` | No | `300.0` | Maximum retry backoff ceiling |
| `NINJATECH_WORKER_MAX_RESULT_BYTES` | No | `262144` | Maximum canonical JSON result bytes |
| `NINJATECH_ENABLE_DIAGNOSTIC_HANDLER` | No | `false` | Enables non-production diagnostic work |

Pydantic validates these values during application startup. Real credentials belong only in
the environment or an ignored `.env` file.

## Logging

Application and request logs are emitted as one JSON object per line to stdout. Request logs
include the request ID, method, path, status code, and duration. Task lifecycle logs include
the task ID, type, previous status, and new status. Query strings, request bodies, complete
task input, results, raw or hashed lease-token material, idempotency data, credentials,
exception messages, authorization headers, and SQL bound parameters are excluded. Worker
events correlate execution with the public-safe `attempt_id`. Heartbeats are DEBUG-level to
avoid routine INFO log noise.

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
  readiness, task persistence, idempotency, worker leases, fencing, recovery, constraints,
  atomic attempt history, and concurrency coverage.
- The container smoke job validates the Compose configuration, builds the application image,
  confirms its runtime user is non-root, waits for PostgreSQL, explicitly applies migrations,
  starts the API and diagnostic worker, checks `/health` and `/ready`, executes success,
  retry-then-success, and cooperative-cancellation tasks, and checks their persisted states.

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
table and global idempotency uniqueness. `0003_reliable_worker_execution` adds task
execution/lease fields, maintainable state constraints, claim and expiry indexes, and the
durable `task_attempts` table. Its downgrade removes only Milestone 3 schema objects; no
migration inserts fake data.
