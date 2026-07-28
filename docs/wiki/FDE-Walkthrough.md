# Customer delivery walkthrough

This page translates the repository's execution primitives into a bounded customer
workflow.

**Status:** delivery design. The service foundation and worker are implemented on `main`;
the Jira, GitHub, service-catalog, and Slack connectors described below are not.

## Customer problem

A software company wants to reduce the manual work required to assemble deployment
context:

- work originates in Jira;
- ownership and policy live in an internal service catalog;
- source and issue context live in GitHub;
- operators coordinate through Slack;
- security requires scoped identities and reviewable evidence;
- retries and process failures must not create duplicate business effects.

The first objective is not broad autonomy. It is one narrow workflow with measurable manual
work displaced and explicit conditions for safe publication.

## Discovery

### Workflow

- What event starts the work?
- Who performs each step today?
- Which system is authoritative for each fact?
- What decision must remain human?
- Which failures create material delay or customer risk?

### Data and authority

- Where does each required input live?
- Which fields are reliable enough to drive a decision?
- How are records versioned?
- What data is restricted or regulated?
- Who authorizes repository access and publication?
- What evidence may be retained, and for how long?

### Acceptance

- What measurable outcome should improve?
- What is the maximum acceptable false-publication rate?
- What evidence is required before production approval?
- How quickly must the first useful workflow complete?
- What result should cause the customer to expand or stop the pilot?

## Proposed workflow contract

> Given one approved service, Jira issue, GitHub repository, and GitHub issue, produce a
> source-linked deployment-context decision and, when policy permits, publish one
> authoritative GitHub comment plus an optional Slack notification.

Allowed:

- read one configured service record;
- read one named Jira issue;
- read one authorized GitHub repository and issue;
- publish one deterministic GitHub comment;
- notify one configured Slack channel;
- retain bounded normalized evidence and action history.

Prohibited:

- arbitrary URLs or repositories;
- code modification or pull-request merge;
- production deployment;
- Jira writes;
- shell execution;
- caller-supplied arbitrary message content;
- external action without current ownership and policy authority.

## Operational architecture

The implemented foundation creates an idempotent task and requires explicit approval.
PostgreSQL is authoritative for lifecycle state. A separate worker claims due work using
`SKIP LOCKED`, commits a durable attempt, and executes outside the transaction. Leases and
heartbeats model temporary ownership; fencing rejects an expired worker after recovery.

The proposed integration layer adds a separate external-action ledger. Task retries alone
cannot prove whether GitHub accepted a comment whose response was lost.

## Design decisions

### Why PostgreSQL rather than a separate queue?

The bounded workload already requires PostgreSQL for durable business state. Transactions,
uniqueness, row locking, scheduling, and attempt history cover the current coordination
needs. A dedicated queue becomes justified when measured throughput, isolation, or delivery
requirements exceed this design.

### Why at-least-once rather than exactly-once?

An internal database and an independent provider cannot share a transaction unless the
provider offers an equivalent contract. The system therefore uses at-least-once task
execution, stable action identity, fencing, reconciliation, and human review for uncertain
outcomes.

### Why is GitHub authoritative and Slack secondary?

The GitHub comment is attached to the engineering work item and can be reconciled by
provider ID and stable marker. Slack is a notification channel. Its failure must not erase
or duplicate an authoritative GitHub result.

### Why check policy before fetching all data?

The service catalog identifies the approved repository, owner, classification, and
publication authority. Checking it first avoids retrieving customer data for a request that
is already unauthorized.

## Hard failure case

Scenario:

1. GitHub accepts a comment.
2. The response is lost.
3. The worker crashes before saving the comment ID.
4. A replacement worker receives the task.

Required behavior:

- preserve an `outcome_unknown` external action;
- wait for a bounded reconciliation holdoff;
- look up the stored provider ID when one exists;
- otherwise scan a bounded, complete comment window for the stable marker;
- bind exactly one verified match;
- require human review for multiple matches or incomplete search;
- never blindly create another comment.

This reduces duplicate risk without making an exactly-once claim.

## Security boundaries

- Provider base URLs come from trusted configuration.
- Task input cannot contain credentials or arbitrary URLs.
- Credentials are isolated by connector.
- Every write requires policy authority and a current execution fence.
- Logs exclude customer payloads, credentials, tokens, and unsafe exceptions.
- Source retention is classification-aware and bounded.
- No code merge, deployment, payment, email, or arbitrary shell authority exists.
- The workflow remains disabled outside controlled environments until authentication and
  tenancy are implemented.

## Pilot measures

The initial deployment should record:

- tasks created, approved, blocked, and reviewed;
- time from approval to decision;
- provider latency and failure classification;
- retries, reconciliations, and duplicate actions prevented;
- false-publication and human-review rates;
- active and repeated users;
- manual context-gathering time displaced.

## Rollout

### Sandbox

- simulator providers and synthetic records;
- no production credentials;
- normal, blocked, malformed, retry, cancellation, and ambiguous-write cases.

### Limited pilot

- one service and one approved repository;
- one operator group;
- human review for every decision;
- no automatic publication until acceptance criteria pass.

### Controlled production

- publication only for approved policies and classifications;
- authentication, tenancy, managed identities, and credential revocation;
- monitored error and reconciliation budgets;
- documented rollback, retention, and incident procedures.

## Delivery acceptance

The workflow is ready to expand only when:

1. the customer confirms the normalized evidence is sufficient;
2. blocked paths perform no downstream reads or writes;
3. repeated execution does not duplicate the GitHub action;
4. ambiguous writes reconcile or stop for review;
5. failure evidence is understandable to an operator;
6. measured manual effort falls without an unacceptable review burden.

Until executable evidence meets those conditions, this page remains a design contract—not
a production claim.
