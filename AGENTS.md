# AGENTS.md

## Project scope

This repository is the production foundation for the NinjaTech Enterprise Ticket-to-PR
Agent. Milestone 3 adds reliable, single-task worker execution to the persistent and
idempotent FastAPI/PostgreSQL foundation.

Do not add LLMs, agent frameworks, external integrations, authentication, frontend work,
Redis, Celery, Kafka, SQS, Kubernetes, Terraform, or AWS infrastructure unless a later
milestone explicitly authorizes it.

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
- Prefer the smallest production-credible implementation and avoid speculative abstractions.

## Deferred design

Global idempotency-key uniqueness is temporary because this milestone has no authenticated
tenant. When tenancy is introduced, idempotency uniqueness must become tenant-scoped.

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

## Commands

- `make install`: install locked development dependencies.
- `make run`: run the API locally.
- `make run-worker`: run one worker process locally.
- `make format`: format the repository.
- `make lint`: run Ruff lint checks.
- `make typecheck`: run strict mypy checks.
- `make test`: run pytest.
- `make check`: run all non-mutating quality checks.
- `make migrate`: apply Alembic migrations.
- `make container-smoke`: migrate and exercise the API, PostgreSQL, and explicitly enabled
  diagnostic worker, including success, retry, cancellation, and readiness degradation.

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
