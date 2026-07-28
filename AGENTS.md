# AGENTS.md

## Project scope

This repository is the production foundation for the NinjaTech Enterprise Ticket-to-PR
Agent. Milestone 4 adds one bounded, deterministic enterprise-integration workflow, an
authoritative GitHub action ledger, and optional secondary Slack evidence to the reliable
worker foundation.

Do not add LLMs, agent frameworks, code generation, repository modification, Jira writes,
authentication, frontend work, Redis, Celery, Kafka, SQS, Kubernetes, Terraform, or AWS
infrastructure unless a later milestone explicitly authorizes it.

## Engineering rules

- Support Python 3.12 and newer.
- Keep code typed and run `make check` before handing off changes.
- Keep `/health` independent of external services.
- Make `/ready` fail closed when PostgreSQL cannot be reached.
- Read secrets from environment variables; never commit a real `.env` file.
- Write application logs as JSON to stdout and propagate request IDs.
- Run schema migrations explicitly; do not run them during application startup.
- Keep the container runtime non-root.
- Keep the container smoke test self-cleaning and free of credential output.
- Keep task transition rules centralized in the task domain module.
- Use database uniqueness for create idempotency and row locks for state transitions.
- Claim worker tasks with short `FOR UPDATE SKIP LOCKED` transactions; never hold a
  transaction during handler execution.
- Atomically update the task and exact active attempt for every heartbeat, recovery, and
  finalization; roll back on an unexpected row count.
- Fence every worker write by current worker, attempt number, and lease-token hash.
- Treat heartbeat uncertainty as ownership loss: cancel local work, stop claiming, discard
  late results, and do not finalize.
- Keep customer cancellation, timeout, shutdown, and ownership loss as distinct causes.
- Never log complete task input, results, raw or hashed lease material, idempotency data,
  credentials, exception messages, or SQL bound parameters.
- Keep the diagnostic handler disabled by default and impossible to enable in staging or
  production.
- Keep `deployment_context_sync` disabled by default and impossible to enable in staging or
  production while task creation and approval are unauthenticated.
- Require an explicitly configured GitHub principal when `deployment_context_sync` is
  enabled, and verify returned repository, issue, and Jira identities before any write.
- Reject GitHub pull requests as issue-comment targets.
- Treat the service catalog as authority for data access and publication. Stop before Jira
  or GitHub reads when catalog policy blocks or requires review.
- Never accept provider base URLs, credentials, or arbitrary publication text from task
  input.
- Keep all direct `httpx2` use inside `integrations/http.py`; connectors expose normalized
  domain values and sanitized errors.
- Fence every external-action transition with the current task lease and atomically append
  its exact `external_action_attempts` evidence row.
- After a confirmed provider write, persist external success through the current fence
  before honoring customer cancellation; ownership loss must still block persistence.
- Reserve `outcome_unknown` for writes that may have reached a provider. A failed read-only
  reconciliation is retryable or permanent, never outcome unknown.
- Reconcile a known GitHub comment ID before marker search. Never silently recreate a
  deleted bound comment, write after incomplete pagination, or choose among multiple
  markers.
- Store only decision-relevant normalized provenance. Strip query parameters from source
  URLs and minimize confidential or restricted artifacts.
- Prefer the smallest production-credible implementation and avoid speculative abstractions.

## Deferred design

Global idempotency-key uniqueness is temporary because this milestone has no authenticated
tenant. When tenancy is introduced, idempotency uniqueness must become tenant-scoped.

The deployment action scope, provider credentials, repository/service allowlists, source
retention, and external-action uniqueness are also temporarily deployment-global. They
must all become tenant-scoped when authentication and tenancy are introduced. Personal
tokens must move to customer-installed GitHub Apps, Jira OAuth identities, and managed
secret storage.

## Task lifecycle

- `pending_approval -> approved`
- `approved -> running`
- `running -> approved` only for a scheduled retry
- `running -> succeeded`
- `running -> failed`
- `pending_approval -> cancelled`
- `approved -> cancelled`
- `running -> cancelled` only after durable cooperative cancellation
- `succeeded`, `failed`, and `cancelled` are terminal.
- Public API commands are limited to create, retrieve, approve, and cancel.
- Concurrent commands for one task must lock and serialize that task row.

## Worker guarantees

- Execution is at least once, not exactly once.
- Approval sets `available_at = clock_timestamp()` in the locked transition transaction.
- Claims commit the running task and its numbered attempt before calling a handler.
- Leases allow crash recovery after a row lock has been released.
- Heartbeats are quieter DEBUG events and extend only a currently fenced lease.
- Recovery preserves expired attempts and either cancels, schedules a retry, or fails.
- Retries use bounded exponential equal-jitter backoff and never busy-loop.
- Results must be finite JSON objects within the configured byte limit.
- Shutdown grace expiry and ownership loss deliberately leave the running record for lease
  recovery rather than guessing an outcome.
- Cooperative cancellation cannot interrupt every blocking third-party call; future
  handlers must use bounded timeouts.
- Fencing blocks stale database updates but cannot undo external side effects. Future tools
  require separate idempotency and outcome reconciliation.
- `task_attempts` is durable audit evidence; prior attempts are never overwritten.

## External action guarantees

- Task identity describes one execution request; `action_scope_key` describes one stable
  customer business side effect across independent tasks and retries.
- A decision snapshot binds workflow version, policy version, normalized decision, and
  stable source versions/content hashes before action reservation.
- Same scope and same desired fingerprint replay. Changed content requires a controlled
  revision or human review.
- Write timeouts, connection loss after send, malformed or oversized 2xx responses, and
  ambiguous server failures require reconciliation before reissue.
- `write_started_at` plus `reconcile_not_before` prevents a replacement worker from creating
  immediately after one negative lookup while provider consistency may still be settling.
- An action ledger and fencing prevent duplicate database transitions; they cannot undo an
  external effect or prove provider-level exactly-once behavior.
- Keep GitHub authoritative and Slack secondary. A Slack failure must never erase or reverse
  a confirmed GitHub result.
- Validate Slack feature enablement and the channel allowlist before catalog or provider
  access. Do not construct the Slack connector for tasks that do not request notification.
- Authorize Slack only with exact configured team, bot-user, and optional bot IDs from
  `auth.test`; names are diagnostic only.
- Render one bounded deterministic top-level Slack message with unfurling disabled. Never
  accept caller-supplied message text, Blocks, metadata, or arbitrary URLs.
- Scope Slack actions to trusted deployment, GitHub revision, service, and channel values;
  never use task, attempt, worker, lease, or random IDs as business identity.
- Persist confirmed Slack success before honoring customer cancellation. Ownership loss
  still blocks every stale transition.
- Never automatically resend a Slack `outcome_unknown` action. This checkpoint deliberately
  has no Slack-history reconciliation scope.
- Keep metric labels within typed low-cardinality enums. Task, repository, issue, service,
  channel, resource, customer, URL, and error-message values are forbidden labels.
- Metrics are process-local/log-oriented in this checkpoint; do not add a public endpoint or
  claim durable aggregation.

## Commands

- `make install`: install locked development dependencies.
- `make run`: run the API locally.
- `make run-worker`: run one worker process locally.
- `make format`: format the repository.
- `make lint`: run Ruff lint checks.
- `make typecheck`: run strict mypy checks.
- `make test`: run pytest.
- `make sandbox-test`: run explicitly gated real-provider sandbox tests; these normally skip.
- `make check`: run all non-mutating quality checks.
- `make migrate`: apply Alembic migrations.
- `make container-smoke`: migrate and exercise the API, PostgreSQL, and explicitly enabled
  diagnostic/integration workers and test-only simulator, including policy-first access,
  authoritative-action replay, ambiguous-write reconciliation, delayed acceptance, and
  secondary Slack success/degradation, readiness degradation, and cleanup.

## Production boundary

The integration and Slack handlers remain disabled in staging and production because the
public task API has no authentication or tenancy. Future idempotency keys, credentials,
action scopes, allowlists, metric routing, and retention rules must be tenant-scoped before
production enablement. GitHub personal tokens should become installation tokens, Jira
tokens should become OAuth identities, Slack bot installation should be customer-owned,
and secrets should move to managed storage.

The `customer/` documents describe fictional Northstar Payments. Do not present them as a
real NinjaTech engagement, certification, or completed production deployment. Do not update
the GitHub Wiki from the Checkpoint 4B branch.

<!-- codebase-memory-mcp:start -->
# Codebase Knowledge Graph (codebase-memory-mcp)

This project uses codebase-memory-mcp to maintain a knowledge graph of the codebase.
ALWAYS prefer MCP graph tools over grep/glob/file-search for code discovery.

## Priority Order
1. `search_graph` — find functions, classes, routes, variables by pattern
2. `trace_path` — trace who calls a function or what it calls
3. `get_code_snippet` — read specific function/class source code
4. `query_graph` — run Cypher queries for complex patterns
5. `get_architecture` — high-level project summary

## When to fall back to grep/glob
- Searching for string literals, error messages, config values
- Searching non-code files (Dockerfiles, shell scripts, configs)
- When MCP tools return insufficient results

## Examples
- Find a handler: `search_graph(name_pattern=".*OrderHandler.*")`
- Who calls it: `trace_path(function_name="OrderHandler", direction="inbound")`
- Read source: `get_code_snippet(qualified_name="pkg/orders.OrderHandler")`
<!-- codebase-memory-mcp:end -->
