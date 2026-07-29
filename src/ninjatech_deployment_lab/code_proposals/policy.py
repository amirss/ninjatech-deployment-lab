from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.domain import ModelProviderName
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    ModelProcessingPolicy,
    ServiceCatalogRecord,
)


class ModelPolicyDecision(StrEnum):
    ALLOWED = "allowed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REFUSED = "refused"


class ModelPolicyReason(StrEnum):
    ALLOWED = "allowed"
    POLICY_MISSING = "policy_missing"
    POLICY_CONFLICT = "policy_conflict"
    POLICY_STALE = "policy_stale"
    POLICY_REQUIRES_REVIEW = "policy_requires_review"
    EXTERNAL_PROCESSING_DISABLED = "external_processing_disabled"
    PROVIDER_NOT_ALLOWED = "provider_not_allowed"
    CLASSIFICATION_NOT_ALLOWED = "classification_not_allowed"
    SOURCE_EGRESS_NOT_ALLOWED = "repository_source_egress_not_allowed"


@dataclass(frozen=True, slots=True)
class ModelPolicyEvaluation:
    decision: ModelPolicyDecision
    reason: ModelPolicyReason
    policy: ModelProcessingPolicy | None
    effective_budgets: ContextBudgets | None


def evaluate_model_processing_policy(
    records: tuple[ServiceCatalogRecord, ...],
    *,
    provider: ModelProviderName,
    minimum_policy_version: int,
    application_budgets: ContextBudgets,
    external_provider: bool,
) -> ModelPolicyEvaluation:
    policies = tuple(record.model_processing_policy for record in records)
    if not policies or any(policy is None for policy in policies):
        return _evaluation(
            ModelPolicyDecision.NEEDS_HUMAN_REVIEW,
            ModelPolicyReason.POLICY_MISSING,
        )
    concrete = tuple(policy for policy in policies if policy is not None)
    signatures = {_policy_signature(policy) for policy in concrete}
    if len(signatures) != 1:
        return _evaluation(
            ModelPolicyDecision.NEEDS_HUMAN_REVIEW,
            ModelPolicyReason.POLICY_CONFLICT,
        )
    policy = concrete[0]
    classifications = {record.data_classification for record in records}
    if len(classifications) != 1:
        return _evaluation(
            ModelPolicyDecision.NEEDS_HUMAN_REVIEW,
            ModelPolicyReason.POLICY_CONFLICT,
        )
    classification = next(iter(classifications))
    if policy.policy_version < minimum_policy_version:
        return _evaluation(
            ModelPolicyDecision.NEEDS_HUMAN_REVIEW,
            ModelPolicyReason.POLICY_STALE,
            policy,
        )
    if policy.human_review_required:
        return _evaluation(
            ModelPolicyDecision.NEEDS_HUMAN_REVIEW,
            ModelPolicyReason.POLICY_REQUIRES_REVIEW,
            policy,
        )
    if external_provider and not policy.external_processing_allowed:
        return _evaluation(
            ModelPolicyDecision.REFUSED,
            ModelPolicyReason.EXTERNAL_PROCESSING_DISABLED,
            policy,
        )
    allowed_providers = {item.value for item in policy.allowed_providers}
    if provider.value not in allowed_providers:
        return _evaluation(
            ModelPolicyDecision.REFUSED,
            ModelPolicyReason.PROVIDER_NOT_ALLOWED,
            policy,
        )
    if classification not in policy.allowed_classifications:
        return _evaluation(
            ModelPolicyDecision.REFUSED,
            ModelPolicyReason.CLASSIFICATION_NOT_ALLOWED,
            policy,
        )
    if classification is DataClassification.RESTRICTED and external_provider:
        return _evaluation(
            ModelPolicyDecision.REFUSED,
            ModelPolicyReason.CLASSIFICATION_NOT_ALLOWED,
            policy,
        )
    if external_provider and not policy.repository_source_egress_allowed:
        return _evaluation(
            ModelPolicyDecision.REFUSED,
            ModelPolicyReason.SOURCE_EGRESS_NOT_ALLOWED,
            policy,
        )
    return ModelPolicyEvaluation(
        decision=ModelPolicyDecision.ALLOWED,
        reason=ModelPolicyReason.ALLOWED,
        policy=policy,
        effective_budgets=application_budgets.constrained_by_policy(policy.maximum_context_bytes),
    )


def _policy_signature(policy: ModelProcessingPolicy) -> tuple[object, ...]:
    return (
        policy.external_processing_allowed,
        tuple(sorted(item.value for item in policy.allowed_providers)),
        tuple(sorted(item.value for item in policy.allowed_classifications)),
        policy.repository_source_egress_allowed,
        policy.maximum_context_bytes,
        policy.human_review_required,
        policy.policy_version,
    )


def _evaluation(
    decision: ModelPolicyDecision,
    reason: ModelPolicyReason,
    policy: ModelProcessingPolicy | None = None,
) -> ModelPolicyEvaluation:
    return ModelPolicyEvaluation(
        decision=decision,
        reason=reason,
        policy=policy,
        effective_budgets=None,
    )
