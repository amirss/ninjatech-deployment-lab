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

## Current status

Completed:

1. Production service foundation
2. Persistent idempotent task state machine
3. Reliable worker execution
4. Milestone 4A — authoritative enterprise integration path

Milestone 4A adds a deterministic `deployment_context_sync` workflow across a customer service catalog, GitHub, and Jira. It normalizes messy provider data, checks policy before unnecessary access, preserves minimized source evidence, and creates or reconciles one bounded GitHub comment without blindly repeating an uncertain write.

Next:

- Milestone 4B — secondary Slack delivery, operational metrics, and customer-facing deployment artifacts
- Milestone 5 — a reasoning model operating through the already bounded and verifiable tool layer

The project deliberately builds reliability before introducing an LLM. A later reasoning agent will operate on top of a durable, observable, and permission-bounded execution system rather than replacing one.

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
