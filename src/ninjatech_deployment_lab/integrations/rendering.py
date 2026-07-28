from __future__ import annotations

import html

from ninjatech_deployment_lab.integrations.connectors import safe_marker
from ninjatech_deployment_lab.integrations.domain import (
    DeploymentContextDecision,
    GitHubRepositoryContext,
    JiraWorkItem,
    ServiceCatalogRecord,
)

MAX_COMMENT_BYTES = 32768


def render_github_comment(
    *,
    action_scope_key: str,
    decision: DeploymentContextDecision,
    service: ServiceCatalogRecord,
    github: GitHubRepositoryContext,
    jira: JiraWorkItem,
) -> str:
    """Render bounded, escaped content without raw descriptions or provider payloads."""
    marker = safe_marker(action_scope_key)
    references = sorted(
        decision.source_references,
        key=lambda reference: (
            reference.provider,
            reference.resource_type,
            reference.provider_resource_identifier,
            reference.source_version,
            reference.content_hash,
        ),
    )
    source_lines = "\n".join(
        f"- `{html.escape(reference.provider)}` "
        f"`{html.escape(reference.resource_type)}` "
        f"`{html.escape(reference.provider_resource_identifier)}` "
        f"(version `{html.escape(reference.source_version)}`)"
        for reference in references
    )
    reason_lines = "\n".join(f"- {html.escape(reason)}" for reason in decision.reasons)
    rendered = (
        f"{marker}\n"
        "## Deployment context\n\n"
        f"**Decision:** `{decision.outcome.value}`\n\n"
        f"**Service:** `{html.escape(service.canonical_service_id)}`  \n"
        f"**Repository:** `{html.escape(github.full_name)}`  \n"
        f"**Jira issue:** `{html.escape(jira.key)}`  \n"
        f"**Policy version:** `{decision.policy_version}`  \n"
        f"**Generated:** `{decision.generated_at.isoformat()}`\n\n"
        "### Reasons\n"
        f"{reason_lines}\n\n"
        "### Source references\n"
        f"{source_lines}\n"
    )
    if len(rendered.encode()) > MAX_COMMENT_BYTES:
        raise ValueError("rendered GitHub comment exceeds its safe size limit")
    return rendered
