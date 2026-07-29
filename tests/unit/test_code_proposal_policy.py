from __future__ import annotations

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.domain import ModelProviderName
from ninjatech_deployment_lab.code_proposals.policy import (
    ModelPolicyDecision,
    ModelPolicyEvaluation,
    ModelPolicyReason,
    evaluate_model_processing_policy,
)
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    ModelProcessingPolicy,
    ServiceCatalogRecord,
)


def _record(
    *,
    classification: DataClassification = DataClassification.INTERNAL,
    policy: ModelProcessingPolicy | None = None,
) -> ServiceCatalogRecord:
    return ServiceCatalogRecord(
        service_id="payments",
        canonical_service_id="payments",
        service_owner="team-payments",
        criticality="tier_1",
        approved_repositories=("customer/service",),
        data_classification=classification,
        deployment_policy_version=1,
        automatic_publication_allowed=True,
        source_version="catalog-v1",
        source_url="https://catalog.example/services/payments",
        model_processing_policy=policy,
    )


def _policy(**updates: object) -> ModelProcessingPolicy:
    values: dict[str, object] = {
        "external_processing_allowed": True,
        "allowed_providers": ("recorded", "openai"),
        "allowed_classifications": ("public", "internal", "confidential"),
        "repository_source_egress_allowed": True,
        "maximum_context_bytes": 8192,
        "human_review_required": False,
        "policy_version": 2,
    }
    return ModelProcessingPolicy.model_validate({**values, **updates})


def _evaluate(*records: ServiceCatalogRecord, external: bool = True) -> ModelPolicyEvaluation:
    return evaluate_model_processing_policy(
        records,
        provider=ModelProviderName.OPENAI if external else ModelProviderName.RECORDED,
        minimum_policy_version=2,
        application_budgets=ContextBudgets(maximum_total_source_bytes=16384),
        external_provider=external,
    )


def test_missing_stale_review_and_conflicting_policy_fail_closed() -> None:
    assert _evaluate(_record()).reason is ModelPolicyReason.POLICY_MISSING
    assert (
        _evaluate(_record(policy=_policy(policy_version=1))).reason
        is ModelPolicyReason.POLICY_STALE
    )
    assert (
        _evaluate(_record(policy=_policy(human_review_required=True))).reason
        is ModelPolicyReason.POLICY_REQUIRES_REVIEW
    )
    conflict = _evaluate(
        _record(policy=_policy()),
        _record(policy=_policy(repository_source_egress_allowed=False)),
    )
    assert conflict.decision is ModelPolicyDecision.NEEDS_HUMAN_REVIEW


def test_every_authority_field_participates_in_policy_conflict() -> None:
    changes = (
        {"external_processing_allowed": False},
        {"allowed_providers": ("recorded",)},
        {"allowed_classifications": ("public",)},
        {"repository_source_egress_allowed": False},
        {"maximum_context_bytes": 4096},
        {"human_review_required": True},
        {"policy_version": 3},
    )
    for change in changes:
        result = _evaluate(_record(policy=_policy()), _record(policy=_policy(**change)))
        assert result.reason is ModelPolicyReason.POLICY_CONFLICT


def test_restricted_external_egress_is_refused_and_budget_uses_minimum() -> None:
    restricted = _evaluate(
        _record(
            classification=DataClassification.RESTRICTED,
            policy=_policy(allowed_classifications=("restricted",)),
        )
    )
    assert restricted.decision is ModelPolicyDecision.REFUSED
    allowed = _evaluate(_record(policy=_policy()))
    assert allowed.effective_budgets is not None
    assert allowed.effective_budgets.maximum_total_source_bytes == 8192


def test_recorded_policy_does_not_require_external_egress_authority() -> None:
    result = _evaluate(
        _record(
            policy=_policy(
                external_processing_allowed=False,
                allowed_providers=("recorded",),
                repository_source_egress_allowed=False,
            )
        ),
        external=False,
    )
    assert result.decision is ModelPolicyDecision.ALLOWED
