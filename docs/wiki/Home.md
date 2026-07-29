# NinjaTech Deployment Lab

A production-oriented engineering project demonstrating how autonomous enterprise work can be executed reliably, safely, and transparently.

The project is built incrementally around a realistic Forward Deployed Engineer problem:

> How do we turn an approved customer workflow into durable autonomous execution while handling retries, crashes, cancellation, concurrency, authority boundaries, and external-system uncertainty?

## What this project demonstrates

- Typed Python and FastAPI services
- PostgreSQL-backed idempotency and concurrency control
- Explicit task-state transitions
- Durable worker execution
- Leases, heartbeats, and fencing
- Crash recovery and bounded retries
- Cooperative cancellation
- Migration and rollback discipline
- Structured observability
- Dockerized, non-root execution
- Real PostgreSQL and end-to-end CI validation
- Policy-first enterprise integration
- Business-scoped external-action identity
- Safe reconciliation after ambiguous provider writes
- Workspace-bound Slack identity and channel authorization
- Ledger-first replay with no blind resend after unknown delivery
- Low-cardinality operational metrics
- Customer discovery, security, acceptance, and rollout artifacts

## Current status

Completed:

1. Production service foundation
2. Persistent idempotent task state machine
3. Reliable worker execution
4. Enterprise integrations and safe external actions
   - 4A: authoritative GitHub path
   - 4B: secondary Slack delivery, metrics, and customer package

Milestone 4 now provides a deterministic `deployment_context_sync` workflow across a customer service catalog, GitHub, Jira, and Slack. It checks policy before unnecessary access, normalizes messy provider data, preserves minimized evidence, creates or reconciles one authoritative GitHub comment, and treats Slack as a secondary notification whose failure never erases GitHub truth.

Next:

- Milestone 5A — bounded reasoning and a source-linked code-change proposal
- Milestone 5B — isolated patch validation and explicit human approval
- Milestone 5C — safe branch and pull-request delivery through the external-action ledger
- Milestone 6 — broader evaluations, adversarial cases, and acceptance evidence

The project deliberately built reliability before introducing an LLM. The model will operate through typed, read-only tools and bounded contracts; it will not own state, credentials, permissions, or external-side-effect truth.

## Core design principle

A worker is not successful because it says it succeeded.

Success must be supported by durable state, verified external outcomes, and an audit trail that survives retries and process failures.

## Explore

- [Architecture Overview](Architecture-Overview)
- [Reliable Worker Execution](Reliable-Worker-Execution)
- [Failure Semantics](Failure-Semantics)
- [Enterprise Integration Design](Enterprise-Integration-Design)
- [Forward Deployed Engineering Walkthrough](FDE-Walkthrough)
- [Roadmap](Roadmap)

## Repository

- [Source code](https://github.com/amirss/ninjatech-deployment-lab)
- [Pull requests](https://github.com/amirss/ninjatech-deployment-lab/pulls)
- [GitHub Actions](https://github.com/amirss/ninjatech-deployment-lab/actions)
