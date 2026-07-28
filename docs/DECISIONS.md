# Engineering decisions

These records explain decisions already represented in code. Each includes a revisit
condition so that a current constraint is not presented as universal architecture advice.

## 1. Use PostgreSQL for business state and bounded work coordination

**Context**

The service needs durable task state, atomic idempotency, scheduling, concurrency control,
and execution history. The current workload is bounded and already requires PostgreSQL.

**Decision**

Use PostgreSQL transactions, uniqueness, row locking, database time, and
`FOR UPDATE SKIP LOCKED` for coordination.

**Alternatives considered**

- Redis or Celery would add a second source of operational truth.
- Kafka would add durable delivery infrastructure without removing the need for business
  state or finalization transactions.
- An in-memory queue would not survive restart or coordinate replicas.

**Consequence**

The operating surface stays small and task state remains directly queryable. The design
accepts polling and is not intended for very high-throughput streaming.

**Revisit when**

Measured throughput, workload isolation, priority scheduling, or cross-region delivery
cannot meet a defined service objective with the current database design.

## 2. Promise at-least-once execution and fence stale workers

**Context**

A worker may crash after a claim or lose trusted database connectivity while a handler is
still running.

**Decision**

Commit a numbered attempt and lease before execution. Require the current worker, attempt
number, and token hash for every heartbeat and finalization. Recover expired leases and
allow work to execute again.

**Alternatives considered**

- Holding a database transaction for the entire handler would create long locks and fragile
  connection ownership.
- Treating process-local completion as authoritative would permit late stale writes.
- Claiming exactly-once behavior would be false once an independent provider is involved.

**Consequence**

Internal state remains coherent after crash or ownership transfer, but handlers and future
tools must tolerate repeated execution.

**Revisit when**

A specific provider offers a stronger transactional or idempotency contract that can be
verified end to end.

## 3. Keep migrations separate from application startup

**Context**

Several application replicas may start simultaneously during deployment.

**Decision**

Run Alembic as an explicit delivery step. API and worker startup never mutate schema.

**Alternatives considered**

- Startup migrations are convenient locally but can race, extend startup unpredictably, and
  mix deployment authority with runtime authority.

**Consequence**

Deployments need a separate migration command and rollback plan. In return, runtime replicas
stay stateless with respect to schema control.

**Revisit when**

The deployment platform provides a proven, serialized release phase with equivalent
visibility and rollback behavior.

## 4. Establish the control plane before adding a reasoning model

**Context**

A model can propose work but cannot supply durable ownership, idempotency, authorization,
or evidence merely by producing plausible text.

**Decision**

Build and verify task state, approvals, attempts, leases, failure classification, and
observability before adding an LLM or real provider writes.

**Alternatives considered**

- Starting with an agent demo would show visible behavior sooner but leave crashes, retries,
  and authority boundaries implicit.
- Adding broad frameworks would obscure which guarantees belong to the application.

**Consequence**

The current release is less visually impressive but its reliability claims are executable.
It must not be marketed as an implemented agentic workflow until the model, tools, and
evaluation evidence exist.

**Revisit when**

One bounded customer workflow, typed tool contract, authority model, and failure-oriented
acceptance suite are ready to exercise the substrate.
