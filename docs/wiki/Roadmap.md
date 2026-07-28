# Evidence-gated roadmap

This roadmap separates shipped behavior from design work. An item is complete only when its
code, tests, and runnable evidence are on the default branch.

The dates and scope may change. The evidence gate should not.

## Released foundation

### Milestone 1 — Service foundation

**Status:** Complete

Evidence:

- typed FastAPI application and validated configuration;
- separate liveness and database readiness;
- structured request logging and correlation IDs;
- explicit Alembic migration path;
- locked dependencies and non-root container;
- format, lint, strict-type, unit, PostgreSQL, and container CI.

Review path:

- initial foundation commit;
- `.github/workflows/ci.yml`;
- `scripts/container-smoke.sh`.

### Milestone 2 — Persistent task lifecycle

**Status:** Complete in [PR #1](https://github.com/amirss/ninjatech-deployment-lab/pull/1)

Evidence:

- atomic idempotent creation;
- canonical request fingerprints;
- database-enforced uniqueness;
- explicit lifecycle transitions;
- row-locked approval and cancellation;
- persistence, constraint, replay, and concurrency tests;
- migration upgrade, downgrade, and re-upgrade.

### Milestone 3 — Reliable worker execution

**Status:** Complete in [PR #2](https://github.com/amirss/ninjatech-deployment-lab/pull/2)

Evidence:

- short `SKIP LOCKED` claim transactions;
- durable numbered attempts;
- leases, heartbeats, and execution fencing;
- expiry recovery;
- bounded retry with equal jitter;
- timeout, shutdown, ownership-loss, and cancellation separation;
- atomic task and attempt finalization;
- late-handler-return tests;
- end-to-end success, retry, cancellation, and readiness-degradation proof.

Run the current evidence with:

```bash
make demo
```

## Next validation target

### Milestone 4 — One bounded enterprise workflow

**Status:** Proposed; not implemented on `main`

Target workflow:

> Given an approved service, Jira issue, GitHub repository, and GitHub issue, produce a
> source-linked deployment-context decision and, when policy permits, publish one
> authoritative GitHub comment.

The detailed [enterprise integration design](Enterprise-Integration-Design) is a proposal,
not evidence of shipped behavior.

The milestone will be considered complete only when the repository contains:

1. a strict workflow input contract with no arbitrary URLs or credentials;
2. policy-first access using a configured service catalog;
3. normalized, bounded Jira and GitHub reads;
4. immutable source provenance;
5. a deterministic decision snapshot;
6. a business-scoped external-action ledger;
7. GitHub comment identity and bounded reconciliation;
8. simulator tests for delayed visibility and ambiguous writes;
9. a container demonstration of ready, blocked, review, replay, and recovery paths;
10. documentation that distinguishes simulator proof from live-provider proof.

Slack notification is secondary. It should not delay the authoritative GitHub path or
inflate the first implementation.

## Subsequent work

No later milestone is scheduled until Milestone 4 produces working evidence.

The likely sequence is:

1. **Reasoning layer** — one bounded model call, typed tool contracts, explicit evidence,
   human escalation, and no merge or deployment authority.
2. **Evaluations** — representative success cases, missing evidence, conflicting policy,
   prompt injection, provider failures, false-success checks, repeated-run consistency,
   latency, and cost.
3. **Controlled deployment** — authentication, tenancy, managed identities and secrets,
   environment separation, rollback, retention, and alerts.
4. **Operator surface** — only the task, evidence, action, and review views required by
   observed users.

These are directions, not current product claims.

## Release gate

Every milestone must pass four forms of evidence:

| Gate | Required proof |
| --- | --- |
| Explain | A concise architecture and boundary description |
| Execute | A deterministic, reproducible happy-path demonstration |
| Break | Injected failure with observable safe behavior |
| Modify | One bounded behavior change without losing prior guarantees |

Documentation may explain intended behavior. Only executable evidence can move a milestone
to complete.
