from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from ninjatech_deployment_lab.integrations.connectors import (
    PermanentProviderError,
    _normalize_service_record,
    _retry_after,
    jira_adf_to_text,
    safe_marker,
)
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    DecisionOutcome,
    DecisionReasonCode,
    DeploymentContextDecision,
    DeploymentContextSyncInput,
    GitHubRepositoryContext,
    JiraWorkItem,
    ServiceCatalogRecord,
    canonical_source_url,
    provider_action_url,
    sha256_json,
)
from ninjatech_deployment_lab.integrations.rendering import render_github_comment


def test_deployment_input_is_strict_and_requires_slack_channel() -> None:
    with pytest.raises(ValidationError):
        DeploymentContextSyncInput.model_validate(
            {
                "jira_issue_key": "ENG-123",
                "github_repository": "customer/repository",
                "github_issue_number": 42,
                "service_id": "payments-api",
                "publish_slack_notification": True,
                "unknown": "rejected",
            }
        )


def test_deployment_input_rejects_provider_urls_and_malformed_identifiers() -> None:
    with pytest.raises(ValidationError):
        DeploymentContextSyncInput.model_validate(
            {
                "jira_issue_key": "eng-123",
                "github_repository": "https://github.example/customer/repository",
                "github_issue_number": 42,
                "service_id": "payments-api",
            }
        )


def test_jira_adf_is_flattened_without_assuming_a_string() -> None:
    document = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Deploy"},
                    {"type": "text", "text": " safely"},
                ],
            }
        ],
    }
    assert jira_adf_to_text(document) == "Deploy safely"


def test_hashing_is_deterministic_and_rejects_non_finite_json() -> None:
    assert sha256_json({"b": 2, "a": {"z": 1}}) == sha256_json({"a": {"z": 1}, "b": 2})
    with pytest.raises(ValueError):
        sha256_json({"unsafe": math.inf})


def test_source_urls_are_canonical_and_markers_do_not_expose_scope() -> None:
    scope = "deployment_context_sync:v1:customer:42:7:payments-api:github_comment"
    marker = safe_marker(scope)
    assert scope not in marker
    assert canonical_source_url("https://example.test/a?token=secret#fragment") == (
        "https://example.test/a"
    )


def test_provider_action_url_preserves_safe_fragment_but_removes_query() -> None:
    value = provider_action_url(
        "https://github.example/customer/service/issues/42?access_token=secret#issuecomment-123"
    )
    assert value == ("https://github.example/customer/service/issues/42#issuecomment-123")
    assert canonical_source_url(value) == ("https://github.example/customer/service/issues/42")


@pytest.mark.parametrize(
    "value",
    [
        "https://user:password@github.example/issues/42#issuecomment-123",
        "javascript:alert(1)",
        "https://github.example/issues/42\n#issuecomment-123",
        "https://github.example/issues/42#issuecomment-123|unsafe",
    ],
)
def test_provider_action_url_rejects_credentials_schemes_and_controls(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        provider_action_url(value)


def test_service_catalog_mapping_resolves_legacy_fields_and_repository_case() -> None:
    record = _normalize_service_record(
        {
            "serviceId": "payments-api",
            "canonicalId": "payments-api",
            "legacy_ids": ["payments-legacy"],
            "owner": "team-payments",
            "tier": "tier-1",
            "repo_names": ["Customer/Example-Service"],
            "classification": "INTERNAL",
            "policy_version": 7,
            "auto_publish": True,
            "reviewer": None,
            "allow_automatic_updates": True,
            "etag": "v7",
            "url": "https://catalog.example/services/payments-api?internal=true",
        }
    )
    assert record.approved_repositories == ("customer/example-service",)
    assert record.aliases == ("payments-legacy",)
    assert record.source_url == "https://catalog.example/services/payments-api"


def test_service_catalog_mapping_fails_closed_on_malformed_record() -> None:
    with pytest.raises(PermanentProviderError):
        _normalize_service_record({"serviceId": "payments-api"})


def test_retry_after_is_parsed_and_bounded() -> None:
    assert _retry_after({"retry-after": "120"}, maximum=30) == 30
    assert _retry_after({"retry-after": "invalid"}, maximum=30) is None


def test_comment_rendering_is_deterministic_and_escapes_provider_text() -> None:
    generated = JiraWorkItem.model_validate(
        {
            "key": "ENG-123",
            "title": "<unsafe>",
            "normalized_description_text": "not rendered",
            "status": "Open",
            "updated_at": "2026-07-27T12:00:00Z",
            "source_url": "https://jira.example/ENG-123",
            "source_version": "v1",
        }
    )
    github = GitHubRepositoryContext(
        full_name="customer/example-service",
        repository_id=42,
        visibility="private",
        archived=False,
        default_branch="main",
        default_branch_head_sha="a" * 40,
        issue_number=7,
        issue_state="open",
        issue_title="<unsafe>",
        is_pull_request=False,
        source_url="https://github.example/issues/7",
        source_version="a" * 40,
    )
    service = ServiceCatalogRecord(
        service_id="payments-api",
        canonical_service_id="payments-api",
        service_owner="team",
        criticality="tier-1",
        approved_repositories=("customer/example-service",),
        data_classification=DataClassification.INTERNAL,
        deployment_policy_version=7,
        automatic_publication_allowed=True,
        allow_automatic_updates=True,
        source_version="v7",
        source_url="https://catalog.example/payments-api",
    )
    decision = DeploymentContextDecision(
        outcome=DecisionOutcome.READY,
        reason_codes=(DecisionReasonCode.READY,),
        reasons=("Ready <after> policy.",),
        source_references=(),
        policy_version=7,
        generated_at=generated.updated_at,
    )
    action_scope_key = "deployment_context_sync:v1:test:42:7:payments-api:github_comment"
    first = render_github_comment(
        action_scope_key=action_scope_key,
        decision=decision,
        service=service,
        github=github,
        jira=generated,
    )
    second = render_github_comment(
        action_scope_key=action_scope_key,
        decision=decision,
        service=service,
        github=github,
        jira=generated,
    )
    assert first == second
    assert "Ready &lt;after&gt; policy." in first
    assert "not rendered" not in first
