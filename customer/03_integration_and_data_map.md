# Northstar Payments — Integration and Data Map

Fictional data-flow artifact for the bounded pilot.

| System | Direction | System of record | Retained normalized fields | Excluded |
| --- | --- | --- | --- | --- |
| Service catalog | Read | Policy, ownership, classification, repository authority | Service ID, owner identifier when permitted, tier, approved repositories, policy version and permissions | Credentials, transport metadata, unrestricted raw record |
| GitHub | Read/write one issue | Authoritative deployment-context comment | Repository/issue identity, visibility, archive state, head SHA, issue state, comment ID/URL | Source files, arbitrary repositories, auth headers, full API payload |
| Jira | Read one issue | Work-item context | Key, bounded title/description when classification permits, status, priority, labels, assignee ID, updated version | Attachments, credentials, unrestricted comments, restricted full description |
| Slack | Write one notification | Secondary delivery only | Action status and confirmed channel/timestamp identifier | Message history, raw response, token, message metadata |

## Flow

Task input → service-catalog policy → authorized GitHub/Jira reads → minimized source
artifacts → deterministic decision snapshot → authoritative GitHub action → optional Slack
action → structured task result.

Provider identity is verified with configured GitHub login and exact Slack team/bot IDs.
Source URLs are canonicalized without query parameters. Artifacts retain schema version,
classification, redaction evidence, content hash, and retention deadline. Restricted data
uses the shortest configured retention class and omits full Jira description text.

Task attempts describe worker execution. External-action attempts separately describe
business side-effect transitions. Neither table stores credentials or complete provider
requests.
