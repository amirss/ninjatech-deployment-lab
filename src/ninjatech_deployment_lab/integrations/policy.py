from __future__ import annotations

from dataclasses import dataclass

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    DecisionOutcome,
    DecisionReasonCode,
    DeploymentContextSyncInput,
    ServiceCatalogRecord,
)


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    outcome: DecisionOutcome
    reason_codes: tuple[DecisionReasonCode, ...]
    reasons: tuple[str, ...]
    record: ServiceCatalogRecord | None


def evaluate_static_scope(
    task_input: DeploymentContextSyncInput,
    settings: Settings,
) -> PolicyEvaluation | None:
    """Fail closed before any network access when trusted scope rejects the request."""
    if task_input.service_id.casefold() not in {
        item.casefold() for item in settings.deployment_allowed_service_ids
    }:
        return _result(
            DecisionOutcome.BLOCKED,
            DecisionReasonCode.SERVICE_NOT_ALLOWED,
            "The requested service is outside the configured deployment scope.",
        )
    if task_input.github_repository.casefold() not in {
        item.casefold() for item in settings.deployment_allowed_github_repositories
    }:
        return _result(
            DecisionOutcome.BLOCKED,
            DecisionReasonCode.REPOSITORY_NOT_ALLOWED,
            "The requested repository is outside the configured deployment scope.",
        )
    jira_project = task_input.jira_issue_key.split("-", maxsplit=1)[0]
    if jira_project.casefold() not in {
        item.casefold() for item in settings.deployment_allowed_jira_projects
    }:
        return _result(
            DecisionOutcome.BLOCKED,
            DecisionReasonCode.REPOSITORY_NOT_ALLOWED,
            "The requested Jira project is outside the configured deployment scope.",
        )
    return None


def evaluate_service_catalog(
    task_input: DeploymentContextSyncInput,
    records: tuple[ServiceCatalogRecord, ...],
    settings: Settings,
) -> PolicyEvaluation:
    """Resolve aliases/conflicts and enforce catalog authority before downstream reads."""
    requested = task_input.service_id.casefold()
    candidates = tuple(
        record
        for record in records
        if requested
        in {
            record.service_id.casefold(),
            record.canonical_service_id.casefold(),
            *(alias.casefold() for alias in record.aliases),
        }
    )
    if not candidates:
        return _result(
            DecisionOutcome.BLOCKED,
            DecisionReasonCode.SERVICE_RECORD_MALFORMED,
            "No authoritative service-catalog record resolved the requested service.",
        )
    signatures = {
        (
            record.canonical_service_id.casefold(),
            record.deployment_policy_version,
            record.approved_repositories,
            record.service_owner,
            record.data_classification,
            record.automatic_publication_allowed,
            record.required_reviewer,
        )
        for record in candidates
    }
    if len(signatures) != 1:
        return _result(
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
            DecisionReasonCode.SERVICE_RECORD_CONFLICT,
            "Conflicting service-catalog records require an owner to choose authority.",
        )
    record = max(candidates, key=lambda item: item.source_version)
    if record.service_owner is None:
        return _result(
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
            DecisionReasonCode.SERVICE_OWNER_MISSING,
            "The service catalog does not identify an accountable service owner.",
            record,
        )
    if record.data_classification is DataClassification.RESTRICTED:
        return _result(
            DecisionOutcome.BLOCKED,
            DecisionReasonCode.DATA_CLASSIFICATION_BLOCKED,
            "Restricted service data is not eligible for this automated workflow.",
            record,
        )
    if record.data_classification is DataClassification.CONFIDENTIAL:
        return _result(
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
            DecisionReasonCode.DATA_CLASSIFICATION_BLOCKED,
            "Confidential service data requires a human-reviewed workflow.",
            record,
        )
    if record.deployment_policy_version < settings.deployment_minimum_policy_version:
        return _result(
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
            DecisionReasonCode.POLICY_STALE,
            "The service policy version is older than the configured minimum.",
            record,
        )
    if task_input.github_repository.casefold() not in record.approved_repositories:
        return _result(
            DecisionOutcome.BLOCKED,
            DecisionReasonCode.REPOSITORY_NOT_OWNED_BY_SERVICE,
            "The service catalog does not authorize this repository for the service.",
            record,
        )
    if not record.automatic_publication_allowed:
        return _result(
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
            DecisionReasonCode.AUTOMATIC_PUBLICATION_DISABLED,
            "The service policy does not permit automatic publication.",
            record,
        )
    if record.required_reviewer is not None:
        return _result(
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
            DecisionReasonCode.REVIEWER_REQUIRED,
            "The service policy requires a named reviewer before publication.",
            record,
        )
    return _result(
        DecisionOutcome.READY,
        DecisionReasonCode.READY,
        "Policy and ownership checks permit the bounded deployment-context action.",
        record,
    )


def _result(
    outcome: DecisionOutcome,
    code: DecisionReasonCode,
    reason: str,
    record: ServiceCatalogRecord | None = None,
) -> PolicyEvaluation:
    return PolicyEvaluation(
        outcome=outcome,
        reason_codes=(code,),
        reasons=(reason,),
        record=record,
    )
