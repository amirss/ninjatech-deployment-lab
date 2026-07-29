# Northstar Payments — Security and Permissions

Fictional CTO/CISO review summary for a controlled pilot.

## Control boundaries

- Credentials are connector-specific environment values or mounted files, never task input.
- GitHub requires bounded issue read/comment write access to explicitly allowlisted
  repositories.
- Jira is read-only for one allowlisted project and issue.
- Slack requires identity verification plus `auth.test` and `chat:write` for allowlisted
  channels; no history or deletion scope is requested.
- Provider base URLs are trusted configuration. Task input cannot supply URLs.
- The workflow performs no shell execution, code checkout, source modification, deployment,
  Jira write, or arbitrary publication.

Logs exclude credentials, authorization headers, complete task input, provider payloads,
rendered comments/messages, exception strings, and SQL parameters. Source artifacts retain
only decision-relevant normalized fields with classification, redaction, and retention
metadata.

Cancellation prevents future writes but cannot undo an accepted provider effect. Leases and
fencing prevent stale database updates. `task_attempts` and `external_action_attempts` are
durable append-only application audit history; they are not cryptographically
tamper-evident.

The simulator is test/demo-only and startup fails in staging or production. The current
model assumes one controlled deployment: credentials, action scopes, uniqueness, allowlists,
and retention are not tenant-scoped.

## Not yet implemented

- Inbound authentication
- SSO or SCIM
- Tenant isolation
- Production data-residency controls
- Managed secret service
- Automated retention deletion
- Compliance certification
- Mathematically exactly-once provider writes

No SOC 2, HIPAA, FedRAMP, ISO 27001, or other certification is claimed. Production use must
remain disabled until authentication, tenancy, customer-owned identities, managed secrets,
retention enforcement, and incident procedures are approved.
