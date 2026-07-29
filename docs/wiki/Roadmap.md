# Roadmap

The project grows through explicit milestones. Each milestone adds one new class of production responsibility while preserving the guarantees established earlier.

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

**Status:** Complete

### 4A — Authoritative GitHub path

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
- stable hidden-marker search when no provider ID is known;
- explicit `outcome_unknown` handling for ambiguous writes;
- reconciliation holdoff for in-flight provider actions;
- blocked and human-review outcomes that perform no unauthorized write;
- customer-cancellation and ownership-loss handling after a provider action;
- development/test-only provider simulator.

### 4B — Secondary Slack delivery and customer package

Built:

- Slack notification only after confirmed authoritative GitHub success;
- trusted workspace, bot-user, optional bot, and channel authorization;
- credential-bound identity caching that invalidates after token or principal change;
- workspace-scoped Slack business-action identity;
- ledger-first replay before Slack network access;
- no blind resend after `outcome_unknown`;
- database-authoritative Retry-After and not-before handling for known-unsent failures;
- exact GitHub comment-anchor links in Slack messages;
- low-cardinality process-local/log-oriented metrics;
- fictional Northstar Payments discovery, workflow, data, security, acceptance, and rollout documents;
- optional real-provider sandbox tests excluded from ordinary CI.

Verified on the final Milestone 4B head:

- Ruff formatting and linting;
- strict mypy;
- Alembic upgrade, downgrade, and re-upgrade through `0004_enterprise_integrations`;
- 274 PostgreSQL-backed tests, with optional sandbox tests excluded;
- non-root container runtime;
- GitHub and Slack success;
- independent-task replay without duplicate provider actions;
- ambiguous GitHub reconciliation;
- Slack unknown-outcome no-resend;
- permanent Slack degradation without loss of GitHub truth;
- policy-blocked zero-access behavior;
- cancellation and ownership-loss races;
- database-outage liveness/readiness behavior;
- guaranteed container cleanup.

Core lesson:

> Once a worker changes another system, task retries are not enough. External actions need stable business identity, explicit uncertainty, and reconciliation. Secondary delivery must never corrupt authoritative truth.

## Milestone 5 — Bounded agentic code-change workflow

**Status:** Next

Milestone 5 introduces a reasoning model only after state, authority, external-action identity, and failure semantics are established.

### 5A — Reasoning and source-linked change proposal

Expected scope:

- provider-neutral model protocol;
- one optional real model provider and one deterministic recorded provider;
- hermetic CI with no model network access or secret requirement;
- policy-authorized, read-only repository context;
- bounded tool loop for file search and file reads;
- strict structured proposal output;
- source citations and evidence hashes;
- prompt-injection boundaries;
- persistent agent-run and agent-step evidence;
- no repository modification, command execution, branch push, or pull request.

Core lesson:

> A model may inspect and propose, but the system owns tools, budgets, permissions, evidence, and stopping conditions.

### 5B — Isolated patch validation and human review

Expected scope:

- disposable isolated workspace;
- exact base-commit binding;
- safe unified-diff parsing and application;
- path, file-count, binary, dependency, and size guardrails;
- trusted per-repository test profile;
- bounded test execution without customer credentials or unrestricted network;
- candidate-change artifact and validation evidence;
- explicit human approval before external publication.

Core lesson:

> Generated code is only a proposal until deterministic controls prove what changed and whether the approved tests passed.

### 5C — Branch and pull-request delivery

Expected scope:

- stable business identity for branch/commit and pull-request actions;
- reconciliation after ambiguous push or PR creation;
- provider-resource-ID-first adoption;
- no merge or production-deployment authority;
- cancellation and ownership-loss correctness;
- external-action evidence for each provider side effect.

Core lesson:

> A validated patch and a published pull request are different business effects and require separate authority and reconciliation.

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
- refusal quality;
- human-review measurements;
- cost, latency, and adoption metrics;
- regression scorecard in CI.

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
- source and agent evidence;
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
