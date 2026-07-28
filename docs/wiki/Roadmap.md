# Roadmap

The project grows through explicit milestones. Each milestone adds one new class of production responsibility and keeps the previous guarantees intact.

## Milestone 1 — Production service foundation

**Status:** Complete

Built:

- typed FastAPI application;
- configuration validation;
- structured JSON request logging;
- request correlation IDs;
- PostgreSQL connectivity;
- separate liveness and readiness endpoints;
- Alembic migrations;
- Docker and Compose;
- non-root runtime;
- CI with formatting, linting, typing, tests, and container smoke checks.

Core lesson:

> A small service should be observable, configurable, testable, and reproducible before it becomes an agent system.

## Milestone 2 — Persistent idempotent task state machine

**Status:** Complete

Built:

- persistent tasks;
- required idempotency keys;
- canonical request fingerprints;
- database-enforced uniqueness;
- explicit lifecycle transitions;
- concurrency-safe approve and cancel operations;
- PostgreSQL row locking;
- strict request and response models;
- migration and concurrency tests.

Core lesson:

> A client retry must not create duplicate business work.

## Milestone 3 — Reliable worker execution

**Status:** Complete

Built:

- separate worker process;
- `FOR UPDATE SKIP LOCKED` claiming;
- durable numbered attempts;
- leases and heartbeats;
- fencing tokens;
- expired-lease recovery;
- bounded exponential retry with jitter;
- timeout policy;
- cooperative cancellation;
- graceful shutdown;
- atomic task and attempt finalization;
- safe diagnostic handler;
- full PostgreSQL and container smoke coverage.

Core lesson:

> Long-running autonomous work needs temporary ownership, recovery, and stale-worker protection.

## Milestone 4 — Enterprise integrations and safe external actions

**Status:** Checkpoint 4A complete; Checkpoint 4B next

### 4A — Authoritative path

**Status:** Complete

Built:

- deterministic `deployment_context_sync` handler;
- static and service-catalog policy before unnecessary downstream access;
- normalized service-catalog, GitHub, and Jira models;
- bounded async HTTP behavior and connector-specific credentials;
- provider-principal and returned-resource identity checks;
- minimized, versioned source artifacts;
- business-scoped external-action identity independent of task identity;
- deterministic, order-independent decision snapshots;
- append-only external-action transition history;
- exact-comment-ID-first GitHub reconciliation;
- stable hidden marker search when no provider ID is known;
- explicit `outcome_unknown` handling for ambiguous writes;
- reconciliation settlement holdoff for in-flight provider actions;
- blocked and human-review outcomes that perform no unauthorized write;
- customer-cancellation and ownership-loss handling after a provider action;
- development/test-only provider simulator;
- complete migration, PostgreSQL, and container smoke validation.

Verified on the final 4A head:

- Ruff formatting and linting;
- strict mypy;
- Alembic upgrade, downgrade, and re-upgrade;
- 209 PostgreSQL-backed tests with zero skips;
- non-root container runtime;
- successful workflow, independent-task replay, ambiguous-write reconciliation, policy-blocked zero-access behavior, delayed provider acceptance, cancellation, ownership-loss recovery, and database-outage readiness.

Core lesson:

> Once a worker changes another system, task retries are not enough. External actions need stable business identity, explicit uncertainty, and reconciliation.

### 4B — Secondary delivery

**Status:** Planned next

Expected scope:

- Slack notification as a secondary, non-authoritative external action;
- no-blind-resend behavior for ambiguous notification delivery;
- low-cardinality operational metrics;
- concise customer discovery, workflow, security, acceptance, and rollout artifacts;
- final Compose smoke scenarios;
- optional real-provider sandbox tests, disabled in ordinary CI;
- explicit retention and deletion gap documentation.

The authoritative GitHub outcome must remain valid even when Slack is unavailable or ambiguous.

## Milestone 5 — Agentic workflow

**Status:** Planned

Expected scope:

- reasoning-model integration;
- typed tool contracts;
- bounded planning;
- repository investigation;
- explicit evidence collection;
- human approval and escalation;
- no merge or production-deployment authority;
- model-independent control plane.

The model will operate only through tools whose permissions, idempotency, and failure semantics were established earlier.

Core lesson:

> A model proposes and reasons; the system owns authority, state, and verification.

## Milestone 6 — Evaluations and guardrails

**Status:** Planned

Expected scope:

- representative normal cases;
- ambiguous requirements;
- missing data;
- unauthorized repositories;
- conflicting instructions;
- prompt injection in tickets or repository content;
- tool and provider failures;
- false-success detection;
- repeated-run consistency;
- human-review measurements;
- cost, latency, and adoption metrics.

Core lesson:

> An agent is not production-ready because a demo succeeded. It needs a defined acceptance contract and failure-oriented evidence.

## Milestone 7 — Cloud deployment

**Status:** Planned

Expected scope:

- AWS deployment;
- IAM least privilege;
- managed secrets;
- environment separation;
- encrypted storage;
- network boundaries;
- CloudWatch logs and metrics;
- alerts;
- rollback;
- backup, retention, and deletion procedures.

Core lesson:

> Deployment is a customer security and operating model, not merely a container running in the cloud.

## Milestone 8 — Lightweight operator interface

**Status:** Planned

Expected scope:

- minimal TypeScript/React interface;
- task lifecycle;
- approvals and cancellation;
- attempts and failure evidence;
- external-action status;
- source references;
- usage and outcome metrics.

This is intentionally an operator surface, not a polished general-purpose product.

## Milestone 9 — Enterprise security package

**Status:** Planned

Expected scope:

- architecture and data-flow diagrams;
- permission matrix;
- provider and subprocessor inventory;
- retention and deletion;
- incident response;
- isolation model;
- prompt-injection controls;
- sample technical answers for SOC 2 and DPA review;
- OIDC/SSO demonstration and SCIM design.

Core lesson:

> Enterprise adoption requires evidence that the workflow fits the customer’s identity, data, security, and accountability model.

## Milestone 10 — Forward-deployed delivery package

**Status:** Planned

Expected scope:

- mock customer discovery summary;
- workflow contract;
- deployment plan;
- live demo;
- architecture and security walkthrough;
- acceptance report;
- incident postmortem;
- adoption analysis;
- reusable product feedback;
- second-workflow expansion proposal.

Core lesson:

> An FDE succeeds when the customer adopts, renews, and expands—not when the code merely ships.

## Decision discipline

Every milestone ends with four gates:

1. **Explain** — describe the architecture in plain language.
2. **Diagnose** — investigate an injected failure from evidence and logs.
3. **Modify** — make one bounded behavior change without destabilizing prior guarantees.
4. **Defend** — explain authority, failure, recovery, and customer impact.

The roadmap may change when a real customer problem or platform constraint provides stronger evidence. It should not expand simply because another technology is interesting.
