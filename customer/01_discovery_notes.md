# Northstar Payments — Discovery Notes

Fictional deployment-planning artifact. This is not a record of a real customer engagement.

## Current workflow

Release coordinators manually collect a Jira issue, GitHub issue and repository state, and
service-catalog ownership policy. They paste a deployment brief into GitHub, then notify an
operations Slack channel. Service owners and risk reviewers resolve policy exceptions.

## Users and systems

- Release coordinators initiate and approve the bounded workflow.
- Service owners verify ownership and deployment context.
- Risk reviewers handle restricted data, stale policy, or uncertain provider outcomes.
- Systems: customer service catalog, Jira, GitHub Issues, Slack, and this application.

## Pain and failure points

- Evidence is copied inconsistently and becomes stale during review.
- Repository ownership and publication authority are checked manually.
- Retried writes can duplicate comments or notifications.
- Provider timeouts make it unclear whether an action happened.
- A coordinator spends an estimated 15–25 minutes preparing each brief.

High-risk actions are publishing to the wrong repository, exposing restricted Jira content,
and repeating an external write after an ambiguous timeout.

## Pilot objective

For an allowlisted sandbox service, produce a policy-backed GitHub brief in under two
minutes, prevent duplicate authoritative comments across retries, and report Slack delivery
truthfully without exposing credentials or full source payloads.

## Open questions

- Which customer role may approve production execution?
- What policy-version age is acceptable per service tier?
- What retention periods apply to each data classification?
- Who manually reconciles unknown Slack delivery?
- Which SSO, tenant, residency, and managed-secret controls are required for production?
