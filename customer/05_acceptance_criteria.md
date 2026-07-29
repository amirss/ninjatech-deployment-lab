# Northstar Payments — Acceptance Criteria

Fictional pilot acceptance checklist.

## Functional cases

- Ready: one confirmed GitHub comment; optional Slack success is separately evidenced.
- Blocked: zero GitHub/Jira access when catalog policy already forbids it; zero writes.
- Human review: missing owner, stale/conflicting policy, or incomplete evidence produces no
  write.
- Provider identity mismatch: no publication.
- Rate limit: bounded delay classification; no busy loop.
- Ambiguous GitHub write: reconcile before reissue and produce no duplicate in the simulator.
- Ambiguous Slack write: keep GitHub success, mark Slack `outcome_unknown`, never auto-resend.
- Customer cancellation: no future write; already confirmed provider truth remains recorded.
- Ownership loss: stale worker cannot mutate action or action-attempt evidence.
- Duplicate prevention: independent tasks reuse the same business-scoped actions.
- Database/provider outage: safe retry or degraded result; `/ready` fails when PostgreSQL is
  unavailable while `/health` remains live.

## Security and privacy

- No credentials, authorization headers, complete provider payloads, rendered messages, or
  SQL parameters in logs, API results, or artifact records.
- Only allowlisted service, repository, Jira project, and Slack channel identifiers proceed.
- Restricted artifacts omit full Jira descriptions and carry bounded retention metadata.
- Runtime image uses a non-root user; simulator cannot run in staging or production.

## Measurable pilot thresholds

- 100% of hermetic acceptance scenarios pass in CI.
- Zero duplicate GitHub comments or Slack messages in replay and ambiguous-write scenarios.
- Zero unauthorized downstream calls in blocked scenarios.
- Ready tasks complete within two minutes under simulator conditions.
- Every external action has ordered append-only transition evidence.
