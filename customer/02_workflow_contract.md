# Northstar Payments — Workflow Contract

Fictional contract for the deterministic `deployment_context_sync` pilot.

## Trigger and bounded input

An approved task names one Jira issue, one GitHub repository and issue, one service ID, and
optionally one allowlisted Slack channel. Provider URLs, credentials, and publication text
cannot come from the task.

## Allowed actions

1. Read and normalize the service-catalog policy first.
2. If policy permits access, read the named GitHub and Jira records.
3. Retain minimized, hashed source evidence.
4. Create or explicitly revise one bounded GitHub issue comment.
5. After GitHub success, optionally post one bounded Slack notification.

GitHub is authoritative. Slack is secondary.

## Prohibited actions

No Jira writes, code or branch changes, pull requests, deployments, shell execution,
arbitrary URLs, unrestricted repository access, caller-authored messages, Slack history
scanning, message deletion, or source attachment downloads.

## Human responsibilities

Operators approve tasks and manage allowlists. Service owners maintain catalog accuracy.
Reviewers resolve stale or conflicting policy, identity mismatch, deleted authoritative
comments, multiple markers, changed evidence, and unknown Slack outcomes.

## Outcomes

- Success: policy is ready and the authoritative GitHub action is confirmed.
- Degraded success: GitHub is confirmed, but Slack is retryable, permanently rejected,
  unknown, or requires review.
- Blocked: policy explicitly forbids access or publication; no downstream write occurs.
- Human review: evidence or provider identity cannot safely authorize automation.

Escalate on identity mismatch, incomplete reconciliation, restricted-data ambiguity,
provider outcome uncertainty, ownership loss, or repeated service outage.
