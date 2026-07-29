# NinjaTech Deployment Lab

NinjaTech Deployment Lab is an incremental, production-oriented deployment engineering
project. Milestone 4 adds one disabled-by-default, deterministic
`deployment_context_sync` workflow to the persistent task and reliable-worker foundation.
Checkpoint 4B keeps GitHub authoritative while adding optional secondary Slack delivery,
process-local operational metrics, and a fictional customer deployment package.

PostgreSQL remains the only queue and coordination system. There is no LLM, agent
framework, authentication, frontend, Redis, Celery, Kafka, SQS, Kubernetes, Terraform, or
AWS infrastructure.

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
- The deployment-context handler checks the internal service catalog before retrieving Jira
  or GitHub data, stores minimized immutable provenance, and uses a fenced external-action
  ledger before creating or updating one bounded GitHub issue comment.
- After that GitHub action is confirmed, an explicitly requested and allowlisted Slack
  notification uses a separate business action and records degraded delivery without
  changing the authoritative result.

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

The `simulator` service is available only through the Compose `integration` profile for
tests and demos:

```bash
docker compose --profile integration up --build
```

It provides fake catalog, Jira, GitHub, and Slack contracts and is rejected by its own
startup guard in staging or production. It is not a production dependency.

The deterministic `diagnostic` handler and the `deployment_context_sync` integration
handler are disabled by default. Because task creation and approval are still
unauthenticated, settings reject the integration handler in staging and production. It may
be enabled only in development, test, demo, or a deliberately controlled sandbox. Slack is
also disabled by default and cannot be enabled unless the workflow, expected workspace and
bot-user IDs, an allowed channel, and exactly one credential source are configured.

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

## Deployment-context workflow

The strict task input is:

```json
{
  "task_type": "deployment_context_sync",
  "input": {
    "jira_issue_key": "ENG-123",
    "github_repository": "customer/example-service",
    "github_issue_number": 42,
    "service_id": "payments-api",
    "publish_slack_notification": true,
    "slack_channel_id": "C1234567890"
  }
}
```

Task input cannot provide provider base URLs or credentials. Trusted settings first enforce
static service, repository, and Jira-project scopes. The handler then fetches and normalizes
the service-catalog record and evaluates ownership, classification, policy freshness,
repository authority, automatic-publication permission, and reviewer requirements. A
`blocked` or `needs_human_review` decision is a successful deterministic task result and
stops before Jira descriptions or GitHub context are fetched.

For a potentially ready request, the handler requires and verifies the configured GitHub
principal using case-insensitive GitHub-login comparison, then fetches bounded GitHub and
Jira context. Returned repository, issue, and Jira identities must match the requested
resources, and pull requests are rejected as issue-comment targets before any write.
Only decision-relevant normalized fields are stored. `source_artifacts` are immutable
observations with content hashes, schema version,
classification, redaction evidence, canonical URLs without query parameters, and bounded
retention. Confidential or restricted policy records are minimized; complete Jira
descriptions are not retained for those classifications.

The authoritative GitHub action identity is not the task ID. It is the trusted business
scope:

```text
deployment_context_sync:v1:{deployment_scope_id}:{repository_id}:{issue_number}:{service_id}:github_comment
```

Independent task submissions for the same business scope therefore converge on one
`external_actions` row. The desired comment fingerprint and deterministic decision snapshot
bind the action to the exact normalized evidence and policy version. Same scope and same
desired state replays the confirmed action. Changed desired state requires an explicit
revision, verified exact comment ID and marker, current publication authority, and an
update-enabled service policy; otherwise the result requires human review.

Before a write, the worker reconciles an exact known comment ID. Only when no reliable ID
exists does it perform a bounded hidden-marker search. A deleted bound comment, a removed
marker, multiple matches, or incomplete pagination requires review and never silently
creates a replacement. Confirmed comment links use a separate action-URL sanitizer that
removes credentials and query parameters while preserving the bounded
`#issuecomment-...` anchor; source-artifact URLs continue to remove all fragments.

A write timeout is not an ordinary retry: the provider may have accepted the comment even
though the worker did not receive a usable response. Such a write becomes
`outcome_unknown`. A replacement worker waits through `reconcile_not_before`, searches for
the provider ID or stable marker, and only issues a create after bounded reconciliation
proves there is no match. Read-only reconciliation failures are retryable but never
`outcome_unknown`.

The action row and an append-only `external_action_attempts` record are updated atomically
under the current task execution fence. This prevents a stale worker from changing either
record, but it cannot undo an effect already accepted by GitHub. Provider-level
exactly-once creation is therefore not mathematically guaranteed; reconciliation reduces
duplicates and converts uncertainty into human review.

### Secondary Slack delivery

GitHub is the authoritative business action. Slack is a secondary notification and begins
only after the GitHub action is confirmed. The connector calls `auth.test` and compares
opaque team, bot-user, and optional bot IDs exactly against trusted settings. Display names
never authorize a write. Successful identity verification is cached in memory only against
the active credential's SHA-256 fingerprint and the complete expected-principal tuple.
Credential rotation or expected-principal changes force a new `auth.test`; neither the
credential nor its fingerprint is logged, persisted, returned, or used in metrics. The
requested channel must be allowlisted before service-catalog or provider access, and message
text is rendered from a fixed bounded template rather than accepted from the caller.

The Slack action scope includes the deployment scope, trusted Slack workspace ID, GitHub
repository and issue, canonical service, confirmed GitHub revision, and channel. It excludes
task, attempt, worker, and lease identifiers. Independent tasks for the same workspace,
GitHub revision, and channel therefore reuse one notification action; a new workspace,
GitHub revision, or channel creates a distinct intent.

The persisted delivery state is one of `not_requested`, `succeeded`,
`retryable_failure`, `outcome_unknown`, `permanent_failure`, or
`needs_human_review`. Reservation and persisted-state replay happen before `auth.test`, so
an existing success, unknown outcome, permanent failure, or review decision does not depend
on current Slack availability. A known pre-transmission failure records
`reconcile_not_before` using PostgreSQL time; `Retry-After` is bounded and persisted, and a
five-second default prevents busy loops when no valid delay is supplied. Before that time,
replay makes no Slack call. An ambiguous write—such as a response lost after transmission or
a malformed 2xx response—is `outcome_unknown` and is never automatically resent because
this checkpoint does not request Slack history scopes or pretend it can reconcile delivery.
Slack failure does not erase, reverse, or misrepresent confirmed GitHub success.

Cancellation before the Slack write prevents it. If Slack confirms success while customer
cancellation is in flight, the fenced action ledger records provider truth before the task
honors cancellation. Ownership loss is different: a stale worker cannot persist success,
and a replacement does not post again.

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
| `NINJATECH_ENABLE_DEPLOYMENT_CONTEXT_SYNC` | No | `false` | Enables the non-production integration workflow |
| `NINJATECH_DEPLOYMENT_SCOPE_ID` | When enabled | None | Trusted business action namespace |
| `NINJATECH_DEPLOYMENT_ALLOWED_SERVICE_IDS` | When enabled | `[]` | JSON list of allowed services |
| `NINJATECH_DEPLOYMENT_ALLOWED_GITHUB_REPOSITORIES` | When enabled | `[]` | JSON list of allowed repositories |
| `NINJATECH_DEPLOYMENT_ALLOWED_JIRA_PROJECTS` | When enabled | `[]` | JSON list of allowed Jira projects |
| `NINJATECH_SERVICE_CATALOG_BASE_URL` | When enabled | Local simulator URL | Trusted catalog endpoint |
| `NINJATECH_JIRA_BASE_URL` | When enabled | Local simulator URL | Trusted Jira endpoint |
| `NINJATECH_GITHUB_BASE_URL` | When enabled | Local simulator URL | Trusted GitHub endpoint |
| `NINJATECH_GITHUB_EXPECTED_LOGIN` | When enabled | None | Expected GitHub principal, compared case-insensitively |
| `NINJATECH_ENABLE_SLACK_NOTIFICATION` | No | `false` | Enables controlled secondary Slack delivery |
| `NINJATECH_SLACK_BASE_URL` | When Slack enabled | Local simulator URL | Trusted Slack API base URL |
| `NINJATECH_SLACK_BOT_TOKEN` | One credential source | None | Slack bot token from the environment |
| `NINJATECH_SLACK_BOT_TOKEN_FILE` | One credential source | None | Absolute mounted Slack token-file path |
| `NINJATECH_SLACK_EXPECTED_TEAM_ID` | When Slack enabled | None | Exact expected Slack workspace ID |
| `NINJATECH_SLACK_EXPECTED_USER_ID` | When Slack enabled | None | Exact expected Slack bot-user ID |
| `NINJATECH_SLACK_EXPECTED_BOT_ID` | No | None | Exact expected Slack bot ID when configured |
| `NINJATECH_DEPLOYMENT_ALLOWED_SLACK_CHANNELS` | When Slack enabled | `[]` | JSON list of allowed channel IDs |
| `NINJATECH_SLACK_MAX_TEXT_CHARS` | No | `1000` | Maximum rendered notification characters |
| `NINJATECH_SLACK_WRITE_TIMEOUT_SECONDS` | No | `5.0` | Bounded Slack write duration |
| `NINJATECH_INTEGRATION_PROVIDER_WRITE_TIMEOUT_SECONDS` | No | `8.0` | Bounded provider write duration |
| `NINJATECH_INTEGRATION_SETTLEMENT_DELAY_SECONDS` | No | `3.0` | Holdoff before ambiguous-write reconciliation |

Pydantic validates these values during application startup. Provider tokens can come from
their dedicated environment variables or corresponding mounted `*_TOKEN_FILE` settings,
never both. Real credentials do not belong in source, task input, artifacts, results, logs,
or CI output.

## Logging

Application and request logs are emitted as one JSON object per line to stdout. Request logs
include the request ID, method, path, status code, and duration. Task lifecycle logs include
the task ID, type, previous status, and new status. Query strings, request bodies, complete
task input, results, raw or hashed lease-token material, idempotency data, credentials,
exception messages, authorization headers, and SQL bound parameters are excluded. Worker
events correlate execution with the public-safe `attempt_id`. Heartbeats are DEBUG-level to
avoid routine INFO log noise.

Operational metrics use a typed, low-cardinality sink. Provider, operation, outcome,
decision, action status, and Slack delivery state are allowed labels. Task IDs, repository
names, issue numbers, service IDs, channel IDs, resource IDs, URLs, and error messages are
forbidden metric labels. The default sink emits allowlisted structured metric events;
in-memory counters are available for tests. Metrics are process-local/log-oriented and
reset after restart—this checkpoint does not add a metrics service or public metrics
endpoint.

## Quality commands

```bash
make format        # Apply Ruff formatting
make format-check  # Check formatting without changing files
make lint          # Run Ruff lint rules
make typecheck     # Run strict mypy checks
make test          # Run unit and integration-style tests
make sandbox-test  # Run opt-in real-provider sandbox tests (normally skipped)
make check         # Run all non-mutating checks
```

Ordinary CI excludes the `sandbox` marker and runs Ruff formatting, Ruff lint, strict mypy,
the Alembic round trip, and all PostgreSQL tests. Container smoke builds the non-root image,
starts PostgreSQL, API, worker, and the development/test-only provider simulator, then
proves GitHub-plus-Slack success, independent replay, unknown-outcome no-resend, permanent
Slack degradation, policy-blocked zero-access, readiness failure, and guaranteed cleanup.

Real-provider tests require `NINJATECH_RUN_SANDBOX_TESTS=true`, the explicit ownership
confirmation `NINJATECH_SANDBOX_OWNER_CONFIRMATION=amirss`, and Amir-controlled resources.
Writes additionally require `NINJATECH_RUN_SANDBOX_WRITES=true`. A write run creates a
distinctively marked GitHub comment and Slack message that require manual cleanup; no
credentials are printed.

## Customer deployment package

The [customer](customer/) directory contains six concise artifacts for the fictional
customer **Northstar Payments**:

1. [Discovery notes](customer/01_discovery_notes.md)
2. [Workflow contract](customer/02_workflow_contract.md)
3. [Integration and data map](customer/03_integration_and_data_map.md)
4. [Security and permissions](customer/04_security_and_permissions.md)
5. [Acceptance criteria](customer/05_acceptance_criteria.md)
6. [Rollout and rollback plan](customer/06_rollout_and_rollback_plan.md)

They are deployment examples, not a claim of a real NinjaTech or customer engagement.
The GitHub Wiki is intentionally not updated on this branch.

The real PostgreSQL readiness, persistence, constraint, and concurrency tests skip when
`NINJATECH_TEST_DATABASE_URL` is absent. GitHub Actions provides PostgreSQL and always
executes them.

## Continuous integration

GitHub Actions has two independent jobs:

- The quality job checks Ruff formatting and linting, runs strict mypy, verifies an Alembic
  upgrade/downgrade/re-upgrade cycle, and runs all non-sandbox pytest tests including live
  PostgreSQL readiness, task persistence, idempotency, worker leases, fencing, recovery,
  constraints, atomic attempt history, and concurrency coverage.
- The container smoke job validates the Compose configuration, builds the application image,
  confirms its runtime user is non-root, waits for PostgreSQL, explicitly applies migrations,
  starts the API and diagnostic worker, checks `/health` and `/ready`, executes success,
  retry-then-success, and cooperative-cancellation tasks. Milestone 4 also starts the
  test/demo-only provider simulator, proves policy-first short-circuiting, creates and
  replays one authoritative GitHub comment, reconciles an ambiguous successful write, and
  verifies delayed acceptance across task lease expiry does not create a duplicate.
  Checkpoint 4B additionally proves Slack success, independent replay, unknown-outcome
  no-resend, permanent Slack degradation without GitHub loss, and blocked zero-access.

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

`0004_enterprise_integrations` adds immutable `source_artifacts`, business-scoped
`external_actions`, and append-only `external_action_attempts`. Its only indexes support
task evidence lookup and ordered action history; it inserts no customer or simulator data.
Checkpoint 4B reuses those provider-neutral tables, so no `0005` migration is required.
