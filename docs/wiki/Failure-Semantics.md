# Failure Semantics

Reliable autonomous work depends less on the happy path than on correctly distinguishing failure states.

The project avoids a common mistake: treating every interrupted coroutine, HTTP exception, or missing response as the same business outcome.

## Core rule

> Do not infer business truth from local process behavior.

A worker timing out does not prove an external action failed. A handler returning does not prove the worker still owns the task. A task retry does not make an external write safe to repeat.

## Failure categories

### Retryable task failure

A temporary condition prevented completion, and no permanent business constraint is known.

Examples:

- temporary provider outage during a read;
- bounded database or network interruption;
- rate limit with a valid retry window;
- handler timeout while attempts remain.

Persisted behavior: return the task to `approved` with a future `available_at`.

### Permanent task failure

The workflow cannot succeed without changing its input, configuration, policy, or code.

Examples:

- malformed task contract;
- unauthorized repository;
- invalid provider response contract;
- result that is non-JSON or exceeds the configured limit;
- exhausted attempt budget.

Persisted behavior: `failed` with a sanitized machine-readable reason.

### Customer cancellation

A user has requested that the business work stop.

Persisted behavior:

- pending or approved tasks become `cancelled` immediately;
- running tasks receive a durable cancellation request;
- the current worker finalizes `cancelled` only if it still owns the execution fence;
- recovery finalizes cancellation after a crash.

### Worker shutdown

The process is stopping for operational reasons. This is not a customer decision and not a task failure.

Persisted behavior after the grace period: no outcome. The lease expires and recovery decides.

### Execution ownership loss

The worker can no longer prove that it owns the current attempt.

Possible causes:

- heartbeat update affected zero rows;
- database connectivity became uncertain;
- lease expired;
- token or attempt mismatch;
- another worker recovered the task.

Persisted behavior: none from the stale worker. It cancels local work, discards results, and stops claiming.

### Outcome unknown

This becomes important when external integrations are added.

`outcome_unknown` means:

> The external write may have reached the provider, but the system cannot yet prove whether the side effect occurred.

Examples:

- GitHub accepted a comment but the connection dropped before the response arrived;
- the provider returned a malformed `2xx` after applying the action;
- the worker crashed after transmitting the request but before persisting the provider resource ID.

It does **not** mean a reconciliation read failed before another write was attempted. That is a retryable reconciliation failure.

## Why reads and writes differ

A read normally has no side effect, so repeating it after a timeout is usually safe.

A write may already have changed another system. Repeating it because the client did not receive a response can create a duplicate business action.

Therefore:

- reads use ordinary bounded retry classification;
- writes require stable action identity;
- ambiguous writes move to reconciliation;
- another write is prohibited until reconciliation is complete.

## Task identity versus action identity

A task describes one execution request inside this service.

An external action describes one intended business-side effect in another system.

Those are not the same identity.

A task may retry many times while the intended GitHub comment remains one logical action. A
separately submitted task may target the same business action. The proposed Milestone 4
design therefore specifies a business-scoped action key rather than relying only on task ID.

## Why fencing is necessary but insufficient

A fencing token prevents a stale worker from changing PostgreSQL after ownership moved elsewhere.

It cannot cancel an HTTP request already in flight and cannot erase a comment GitHub already accepted.

This creates a dangerous race:

1. Worker A sends a write.
2. Its lease expires while the provider is processing it.
3. Worker B takes ownership.
4. The first action is not yet visible during reconciliation.
5. Worker B could create a duplicate.
6. The first provider request later completes.

Mitigations include:

- stable hidden markers;
- provider-resource-ID lookup;
- `write_started_at` evidence;
- a reconciliation holdoff window;
- bounded complete search;
- human review when the provider remains inconclusive.

These measures reduce duplicate risk but do not create a mathematical exactly-once guarantee across independent systems.

## Atomic internal finalization

For every internal execution outcome, the task row and exact attempt row are updated in one transaction.

The transaction verifies:

- active task state;
- current worker;
- attempt number;
- token hash;
- unexpired lease;
- matching active attempt.

If either expected row is missing or inconsistent, the entire transaction rolls back.

This prevents the public task from saying “succeeded” while its attempt history says “running,” or the reverse.

## Late handler returns

A coroutine may catch `CancelledError` and return normally. The runner therefore tracks the original cancellation cause outside the handler result.

A late return cannot become success after:

- customer cancellation;
- timeout;
- shutdown grace expiry;
- ownership loss.

The original control-plane cause remains authoritative.

## Failure table

| Failure | Expected behavior |
|---|---|
| Crash after claim, before handler | Lease expires; attempt preserved; recovery decides |
| Crash during handler | Same recovery path; execution may repeat |
| Database outage during heartbeat | Worker stops trusting ownership and does not finalize |
| Database outage during completion | No terminal claim; recovery decides later |
| Stale worker returns after recovery | Fenced update rejected; result discarded |
| Customer cancels near success | Database serialization produces one valid outcome |
| Handler ignores cancellation | Lease eventually expires; no false success |
| Retry storm | Bounded exponential backoff with jitter |
| Unsupported task type | Remains approved and unclaimed |
| No workers available | Durable tasks wait in PostgreSQL |
| External write accepted, response lost | Outcome unknown; reconcile before reissue |
| Multiple matching external markers | Human review; do not delete automatically |
| Known external resource deleted manually | Human review; do not silently recreate |

## Interview takeaway

A production engineer should be able to answer not only “What happens when this succeeds?” but also:

- What exactly do we know after this failure?
- Which component is authoritative?
- Can the action be repeated safely?
- Who currently owns execution?
- What evidence survives the crash?
- What decision requires a human?

Those questions drive the architecture of this project.
