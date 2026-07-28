from __future__ import annotations

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    DecisionOutcome,
    DecisionReasonCode,
    DeploymentContextSyncInput,
    ServiceCatalogRecord,
)
from ninjatech_deployment_lab.integrations.policy import (
    evaluate_service_catalog,
    evaluate_static_scope,
)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        environment="test",
        enable_deployment_context_sync=True,
        deployment_scope_id="test-scope",
        deployment_allowed_service_ids=("payments-api",),
        deployment_allowed_github_repositories=("customer/example-service",),
        deployment_allowed_jira_projects=("ENG",),
        deployment_minimum_policy_version=7,
        github_expected_login="simulator-bot",
    )


def _task_input() -> DeploymentContextSyncInput:
    return DeploymentContextSyncInput(
        jira_issue_key="ENG-123",
        github_repository="Customer/Example-Service",
        github_issue_number=42,
        service_id="payments-api",
    )


def _record(**changes: object) -> ServiceCatalogRecord:
    values: dict[str, object] = {
        "service_id": "payments-api",
        "canonical_service_id": "payments-api",
        "aliases": ("payments-legacy",),
        "service_owner": "team-payments",
        "criticality": "tier-1",
        "approved_repositories": ("customer/example-service",),
        "data_classification": DataClassification.INTERNAL,
        "deployment_policy_version": 7,
        "automatic_publication_allowed": True,
        "required_reviewer": None,
        "allow_automatic_updates": True,
        "source_version": "v7",
        "source_url": "https://catalog.example/services/payments-api",
    }
    values.update(changes)
    return ServiceCatalogRecord.model_validate(values)


def test_repository_matching_is_case_insensitive() -> None:
    result = evaluate_service_catalog(_task_input(), (_record(),), _settings())
    assert result.outcome is DecisionOutcome.READY


def test_missing_owner_and_stale_policy_require_review() -> None:
    missing = evaluate_service_catalog(
        _task_input(),
        (_record(service_owner=None),),
        _settings(),
    )
    stale = evaluate_service_catalog(
        _task_input(),
        (_record(deployment_policy_version=6),),
        _settings(),
    )
    assert missing.reason_codes == (DecisionReasonCode.SERVICE_OWNER_MISSING,)
    assert stale.reason_codes == (DecisionReasonCode.POLICY_STALE,)


def test_conflicting_records_require_review() -> None:
    result = evaluate_service_catalog(
        _task_input(),
        (_record(source_version="v7"), _record(deployment_policy_version=8)),
        _settings(),
    )
    assert result.reason_codes == (DecisionReasonCode.SERVICE_RECORD_CONFLICT,)


def test_conflicting_automatic_update_authority_requires_review() -> None:
    result = evaluate_service_catalog(
        _task_input(),
        (
            _record(source_version="v7", allow_automatic_updates=True),
            _record(source_version="v8", allow_automatic_updates=False),
        ),
        _settings(),
    )
    assert result.reason_codes == (DecisionReasonCode.SERVICE_RECORD_CONFLICT,)


def test_static_scope_blocks_before_provider_access() -> None:
    task_input = _task_input().model_copy(update={"service_id": "other-service"})
    result = evaluate_static_scope(task_input, _settings())
    assert result is not None
    assert result.outcome is DecisionOutcome.BLOCKED


def test_static_scope_uses_a_jira_specific_reason() -> None:
    task_input = _task_input().model_copy(update={"jira_issue_key": "OPS-123"})
    result = evaluate_static_scope(task_input, _settings())
    assert result is not None
    assert result.reason_codes == (DecisionReasonCode.JIRA_PROJECT_NOT_ALLOWED,)
