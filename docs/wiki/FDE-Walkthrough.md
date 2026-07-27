# Forward Deployed Engineering Walkthrough

This page presents the project as a customer deployment rather than as a collection of backend features.

## The customer problem

A large software company wants autonomous engineering workflows, but its environment is messy:

- work originates in Jira;
- service ownership and policy live in an internal catalog;
- source and issue context live in GitHub;
- operators communicate through Slack;
- security requires tightly scoped identities, complete evidence, and no uncontrolled actions;
- retries and process failures must not create duplicate business effects.

The deployment objective is to deliver one narrow workflow safely, prove adoption and reliability, and create a foundation for expansion.

## Discovery questions

An FDE should begin by understanding the real operation, not by proposing agents immediately.

### Workflow

- What event starts the work?
- Who performs each step today?
- Which system is authoritative at each step?
- Where do people copy data manually?
- Which failures cause delay, rework, or customer risk?
- What must remain a human decision?

### Data

- Where does each input live?
- Which fields are reliable?
- How are records versioned?
- Which data is restricted or regulated?
- Who can authorize access?
- What should not be retained?

### Success

- What measurable outcome should improve?
- How quickly must the first useful workflow ship?
- What error is unacceptable?
- What evidence is required for production approval?
- What would cause the customer to expand the deployment?

## Workflow contract

The first workflow is intentionally bounded:

> Given one approved service, Jira issue, GitHub repository, and GitHub issue, produce a source-linked deployment-context decision and, when policy permits, publish one authoritative GitHub comment plus an optional Slack notification.

### Allowed

- read one configured service record;
- read one named Jira issue;
- read one authorized GitHub repository and issue;
- publish one deterministic GitHub comment;
- notify one configured Slack channel;
- retain bounded normalized evidence and audit history.

### Prohibited

- arbitrary URLs;
- arbitrary repository access;
- code modification;
- pull-request merge;
- production deployment;
- Jira writes;
- shell execution;
- arbitrary user-supplied message content;
- external action without current ownership and policy authority.

## Architecture narrative

A concise interview explanation:

> The API creates an idempotent task and requires explicit approval. PostgreSQL is the durable source of truth for lifecycle state. A separate worker claims due work using `SKIP LOCKED`, commits a durable attempt, and executes outside the transaction. Leases and heartbeats model temporary ownership; fencing prevents an expired worker from committing after recovery. Milestone 4 adds a separate external-action ledger because task retries alone cannot prevent duplicate side effects in GitHub or Slack.

## Key design decisions

### Why PostgreSQL instead of adding a queue product?

The bounded workload already needs PostgreSQL for durable business state. It provides transactions, uniqueness, row locks, scheduling, and attempt history. Keeping one coordination system reduces operational complexity while preserving correctness. A dedicated queue becomes justified later if throughput, isolation, or delivery patterns exceed this design.

### Why at-least-once rather than exactly-once?

Exactly-once behavior cannot be guaranteed across an internal database and independent external providers without provider-supported idempotency or a distributed transaction. The system instead provides at-least-once task execution, stable external action identity, fencing, reconciliation, and explicit unknown-outcome handling.

### Why is GitHub authoritative and Slack secondary?

The GitHub comment is attached to the engineering work item and can be reconciled through a resource ID and stable marker. Slack is a notification channel whose delivery may be ambiguous. A Slack failure should not erase a confirmed authoritative GitHub outcome.

### Why check policy before fetching all data?

The customer service catalog defines which repository is approved, the service owner, classification, and publication authority. Checking policy first minimizes data access and avoids retrieving Jira or GitHub content for a workflow that is already unauthorized.

### Why no LLM yet?

Reasoning is valuable only after the execution substrate has durable state, permissions, failure recovery, auditability, and safe tool semantics. The project builds those guarantees first so a later model cannot bypass them.

## How to explain the hardest failure

Question:

> GitHub creates the comment, but the response is lost and the worker crashes. What happens?

Answer:

> The external action remains in an executing or outcome-unknown state. A replacement worker receives a new task fence, waits for the configured reconciliation holdoff, then looks up a stored provider ID or scans bounded issue-comment pages for the stable hidden marker. If it finds one verified comment, it binds to that resource and does not create another. If the search is incomplete or multiple comments match, it stops for human review. The system reduces duplicate risk without claiming an impossible cross-provider exactly-once guarantee.

## Security conversation

A customer CTO or CISO should hear clear boundaries:

- provider base URLs come only from trusted configuration;
- task input cannot contain credentials or arbitrary URLs;
- credentials are isolated per connector;
- all writes require policy authority and current execution ownership;
- production environments cannot enable the integration workflow before authentication and tenancy exist;
- logs exclude customer payloads, credentials, tokens, and unsafe exceptions;
- source retention is classification-aware and minimized;
- no code merge, deployment, email, payment, or arbitrary shell authority exists.

## Adoption metrics

An FDE is accountable for customer outcome, not only deployment completion.

The initial workflow should instrument:

- tasks created and approved;
- time from approval to decision;
- decision distribution: ready, blocked, review;
- provider request latency and failure rate;
- retries and reconciliations;
- duplicate actions prevented;
- human review rate;
- Slack delivery degradation;
- active users and repeated use;
- hours of manual context gathering displaced.

## Rollout plan

### Phase 1 — Sandbox

- simulator providers;
- synthetic and redacted records;
- no production credentials;
- normal, blocked, malformed, retry, cancellation, and ambiguous-write cases.

### Phase 2 — Limited pilot

- one customer service;
- one approved repository;
- one small operator group;
- human review for every decision;
- no automatic GitHub publication until acceptance criteria pass.

### Phase 3 — Controlled production

- automatic publication only for approved classifications and policies;
- monitored error and reconciliation budgets;
- documented rollback and credential revocation;
- regular policy and case review.

## Expansion path

After proving deployment-context synchronization, the next adjacent workflow could be incident investigation:

1. receive an approved alert;
2. collect service ownership, recent deployments, logs, and relevant tickets;
3. produce a source-linked investigation brief;
4. open or update a Jira incident;
5. recommend—but never autonomously execute—a rollback;
6. route the decision to the on-call owner.

This expansion reuses the same execution, evidence, authority, and reconciliation primitives while creating a larger customer footprint.

## What this project proves

The repository is intended to demonstrate that the builder can:

- turn an ambiguous business workflow into a precise contract;
- design and implement the production substrate;
- reason about concurrency and distributed failure;
- integrate external systems safely;
- communicate architecture to operators, CTOs, and security teams;
- define measurable adoption and expansion;
- identify limitations rather than hide them.
