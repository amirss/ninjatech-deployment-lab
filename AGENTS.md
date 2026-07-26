# AGENTS.md

## Project scope

This repository is the production foundation for the NinjaTech Enterprise Ticket-to-PR
Agent. Milestone 2 adds only a persistent and idempotent task state machine to the FastAPI,
PostgreSQL, migration, observability, test, and delivery foundation.

Do not add LLMs, agent frameworks, Redis, Celery, Jira, GitHub, Slack, AWS, or
ticket-processing behavior unless a later milestone explicitly authorizes them.

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
- Never log complete task input, raw idempotency keys, or SQL bound parameters.
- Prefer the smallest production-credible implementation and avoid speculative abstractions.

## Deferred design

Global idempotency-key uniqueness is temporary because this milestone has no authenticated
tenant. When tenancy is introduced, idempotency uniqueness must become tenant-scoped.

## Task lifecycle

- `pending_approval -> approved`
- `approved -> running`
- `running -> succeeded`
- `running -> failed`
- `pending_approval -> cancelled`
- `approved -> cancelled`
- `succeeded`, `failed`, and `cancelled` are terminal.
- Public API commands are limited to create, retrieve, approve, and cancel.
- Concurrent commands for one task must lock and serialize that task row.

## Commands

- `make install`: install locked development dependencies.
- `make run`: run the API locally.
- `make format`: format the repository.
- `make lint`: run Ruff lint checks.
- `make typecheck`: run strict mypy checks.
- `make test`: run pytest.
- `make check`: run all non-mutating quality checks.
- `make migrate`: apply Alembic migrations.
- `make container-smoke`: build and exercise the complete Docker Compose stack.

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
