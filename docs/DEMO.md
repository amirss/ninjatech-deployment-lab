# Seven-minute evidence walkthrough

This walkthrough tests the claims implemented on the default branch. It does not simulate
an LLM or pretend that proposed external integrations already exist.

## One-command proof

Prerequisites:

- Docker with Docker Compose;
- `curl`;
- Python 3 available on the host.

Prepare the local environment:

```bash
cp .env.example .env
```

Replace the placeholder password in `.env`, then run:

```bash
make demo
```

The command delegates to the same self-cleaning container smoke script used by CI.

## Evidence sequence

The script performs these checks in order:

| Step | Claim under test | Failure condition |
| --- | --- | --- |
| Compose validation | The declared stack is structurally valid | Invalid configuration |
| Image build | The locked application can be reproduced | Build or dependency failure |
| Runtime identity | The application does not run as root | UID is `0` |
| Migration | Schema change is explicit and applies to a fresh database | Alembic failure |
| Health and readiness | Liveness and database readiness are distinct | Wrong status code |
| Normal task | Approved work can be claimed and finalized | Task does not succeed |
| Retry task | A retryable failure is delayed and attempted again | Wrong attempt count or status |
| Cancellation task | Running cancellation is durable and cooperative | No `202` or terminal cancellation |
| Database outage | The process stays alive while readiness fails closed | `/health != 200` or `/ready != 503` |

The script exits non-zero at the first unmet claim. An exit trap removes the isolated
containers, network, and volumes on success or failure.

## Code paths worth inspecting

- Task lifecycle: `src/ninjatech_deployment_lab/tasks/domain.py`
- Idempotent creation and locked transitions:
  `src/ninjatech_deployment_lab/tasks/repository.py`
- Worker claims, heartbeats, recovery, and finalization:
  `src/ninjatech_deployment_lab/worker/repository.py`
- Timeout, cancellation, shutdown, and ownership-loss precedence:
  `src/ninjatech_deployment_lab/worker/runner.py`
- Real PostgreSQL behavior: `tests/integration/test_worker.py`
- Late-return and cause-precedence behavior: `tests/unit/test_worker_runner.py`
- Container proof: `scripts/container-smoke.sh`

## Three review questions

### Why is the handler executed outside the claim transaction?

A row lock should protect a short state transition, not a long-running operation. The
committed lease represents durable temporary ownership after the transaction ends.

### Why does a database error during heartbeat stop the worker from finalizing?

The worker can no longer prove that it owns the current attempt. Treating uncertainty as
continued authority could allow a stale process to commit after recovery assigned the work
elsewhere.

### Why is this not exactly-once execution?

The database can fence its own state. It cannot atomically coordinate with an independent
external provider. Safe external writes require their own stable business identity and
reconciliation contract.

## Manual fault exploration

The automated proof is intentionally deterministic. For an interactive review:

1. start the stack and apply migrations;
2. create and approve a diagnostic delay task;
3. stop its worker while the task is running;
4. wait for the lease to expire;
5. start a replacement worker;
6. inspect the task and numbered attempts.

The expected result is preserved attempt history plus recovery under a new execution fence,
not an overwritten attempt or a late success from the stopped worker.
