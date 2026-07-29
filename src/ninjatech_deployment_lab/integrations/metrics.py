from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ninjatech_deployment_lab.integrations.domain import (
    DecisionOutcome,
    SlackDeliveryState,
)
from ninjatech_deployment_lab.integrations.model import (
    ExternalActionStatus,
)

logger = logging.getLogger(__name__)


class MetricName(StrEnum):
    PROVIDER_REQUEST_COUNT = "provider_request_count"
    PROVIDER_LATENCY_SECONDS = "provider_latency_seconds"
    PROVIDER_RATE_LIMIT_COUNT = "provider_rate_limit_count"
    POLICY_DECISION_COUNT = "policy_decision_count"
    EXTERNAL_ACTION_OUTCOME_COUNT = "external_action_outcome_count"
    RECONCILIATION_COUNT = "reconciliation_count"
    DUPLICATE_ACTION_PREVENTION_COUNT = "duplicate_action_prevention_count"
    SLACK_DELIVERY_STATE_COUNT = "slack_delivery_state_count"
    OUTCOME_UNKNOWN_COUNT = "outcome_unknown_count"
    MODEL_REQUEST_COUNT = "model_request_count"
    MODEL_LATENCY_SECONDS = "model_latency_seconds"
    AGENT_STEP_COUNT = "agent_step_count"
    REPOSITORY_TOOL_CALL_COUNT = "repository_tool_call_count"
    PROPOSAL_OUTCOME_COUNT = "proposal_outcome_count"
    MODEL_REFUSAL_COUNT = "model_refusal_count"
    MODEL_GUARDRAIL_REJECTION_COUNT = "model_guardrail_rejection_count"
    SOURCE_DRIFT_COUNT = "source_drift_count"
    CONTEXT_BUDGET_EXHAUSTION_COUNT = "context_budget_exhaustion_count"


class MetricLabel(StrEnum):
    PROVIDER = "provider"
    OPERATION = "operation"
    OUTCOME = "outcome"
    DECISION = "decision"
    ACTION_STATUS = "action_status"
    DELIVERY_STATE = "delivery_state"
    PROPOSAL_OUTCOME = "proposal_outcome"
    TOOL = "tool"


class MetricOperation(StrEnum):
    READ = "read"
    IDENTITY = "identity"
    RECONCILIATION = "reconciliation"
    WRITE = "write"
    GITHUB_COMMENT = "upsert_deployment_context_comment"
    SLACK_NOTIFICATION = "post_deployment_context_notification"
    MODEL_COMPLETE = "model_complete"
    PROPOSAL_VALIDATE = "proposal_validate"


class MetricProvider(StrEnum):
    SERVICE_CATALOG = "service_catalog"
    JIRA = "jira"
    GITHUB = "github"
    SLACK = "slack"
    RECORDED = "recorded"
    OPENAI = "openai"


class MetricProposalOutcome(StrEnum):
    PROPOSED = "proposed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REFUSED = "refused"


class MetricTool(StrEnum):
    SEARCH_PATHS = "search_repository_paths"
    READ_FILES = "read_repository_files"


class MetricOutcome(StrEnum):
    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    CONTRACT_FAILURE = "contract_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RATE_LIMITED = "rate_limited"


MetricLabels = Mapping[MetricLabel, StrEnum]


class MetricsSink(Protocol):
    def increment(
        self,
        name: MetricName,
        *,
        labels: MetricLabels,
        amount: int = 1,
    ) -> None: ...

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: MetricLabels,
    ) -> None: ...


_EXPECTED_LABELS: dict[MetricName, frozenset[MetricLabel]] = {
    MetricName.PROVIDER_REQUEST_COUNT: frozenset(
        {MetricLabel.PROVIDER, MetricLabel.OPERATION, MetricLabel.OUTCOME}
    ),
    MetricName.PROVIDER_LATENCY_SECONDS: frozenset(
        {MetricLabel.PROVIDER, MetricLabel.OPERATION, MetricLabel.OUTCOME}
    ),
    MetricName.PROVIDER_RATE_LIMIT_COUNT: frozenset({MetricLabel.PROVIDER, MetricLabel.OPERATION}),
    MetricName.POLICY_DECISION_COUNT: frozenset({MetricLabel.DECISION}),
    MetricName.EXTERNAL_ACTION_OUTCOME_COUNT: frozenset(
        {MetricLabel.PROVIDER, MetricLabel.OPERATION, MetricLabel.ACTION_STATUS}
    ),
    MetricName.RECONCILIATION_COUNT: frozenset({MetricLabel.PROVIDER, MetricLabel.OUTCOME}),
    MetricName.DUPLICATE_ACTION_PREVENTION_COUNT: frozenset(
        {MetricLabel.PROVIDER, MetricLabel.OPERATION}
    ),
    MetricName.SLACK_DELIVERY_STATE_COUNT: frozenset({MetricLabel.DELIVERY_STATE}),
    MetricName.OUTCOME_UNKNOWN_COUNT: frozenset({MetricLabel.PROVIDER, MetricLabel.OPERATION}),
    MetricName.MODEL_REQUEST_COUNT: frozenset(
        {MetricLabel.PROVIDER, MetricLabel.OPERATION, MetricLabel.OUTCOME}
    ),
    MetricName.MODEL_LATENCY_SECONDS: frozenset(
        {MetricLabel.PROVIDER, MetricLabel.OPERATION, MetricLabel.OUTCOME}
    ),
    MetricName.AGENT_STEP_COUNT: frozenset({MetricLabel.OUTCOME}),
    MetricName.REPOSITORY_TOOL_CALL_COUNT: frozenset({MetricLabel.TOOL, MetricLabel.OUTCOME}),
    MetricName.PROPOSAL_OUTCOME_COUNT: frozenset({MetricLabel.PROPOSAL_OUTCOME}),
    MetricName.MODEL_REFUSAL_COUNT: frozenset({MetricLabel.PROVIDER}),
    MetricName.MODEL_GUARDRAIL_REJECTION_COUNT: frozenset({MetricLabel.OUTCOME}),
    MetricName.SOURCE_DRIFT_COUNT: frozenset({MetricLabel.OUTCOME}),
    MetricName.CONTEXT_BUDGET_EXHAUSTION_COUNT: frozenset({MetricLabel.OUTCOME}),
}

_LABEL_TYPES: dict[MetricLabel, type[StrEnum]] = {
    MetricLabel.PROVIDER: MetricProvider,
    MetricLabel.OPERATION: MetricOperation,
    MetricLabel.OUTCOME: MetricOutcome,
    MetricLabel.DECISION: DecisionOutcome,
    MetricLabel.ACTION_STATUS: ExternalActionStatus,
    MetricLabel.DELIVERY_STATE: SlackDeliveryState,
    MetricLabel.PROPOSAL_OUTCOME: MetricProposalOutcome,
    MetricLabel.TOOL: MetricTool,
}


def _safe_labels(name: MetricName, labels: MetricLabels) -> tuple[tuple[str, str], ...]:
    if frozenset(labels) != _EXPECTED_LABELS[name]:
        raise ValueError("metric labels do not match the low-cardinality contract")
    normalized: list[tuple[str, str]] = []
    for key, value in labels.items():
        if not isinstance(value, _LABEL_TYPES[key]):
            raise ValueError("metric label value is outside its allowlisted enum")
        normalized.append((key.value, value.value))
    return tuple(sorted(normalized))


class StructuredLoggingMetricsSink:
    """Process-local metrics emitted as allowlisted structured log records."""

    def increment(
        self,
        name: MetricName,
        *,
        labels: MetricLabels,
        amount: int = 1,
    ) -> None:
        if amount < 1:
            raise ValueError("counter increments must be positive")
        self._emit(name, "counter", float(amount), labels)

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: MetricLabels,
    ) -> None:
        if value < 0:
            raise ValueError("metric observations must be non-negative")
        self._emit(name, "observation", value, labels)

    @staticmethod
    def _emit(
        name: MetricName,
        kind: str,
        value: float,
        labels: MetricLabels,
    ) -> None:
        safe = _safe_labels(name, labels)
        logger.info(
            "operational_metric",
            extra={
                "event": "operational_metric",
                "metric_name": name.value,
                "metric_kind": kind,
                "metric_value": value,
                "metric_labels": dict(safe),
            },
        )


@dataclass(slots=True)
class InMemoryMetricsSink:
    counters: dict[tuple[MetricName, tuple[tuple[str, str], ...]], int] = field(
        default_factory=dict
    )
    observations: list[tuple[MetricName, float, tuple[tuple[str, str], ...]]] = field(
        default_factory=list
    )

    def increment(
        self,
        name: MetricName,
        *,
        labels: MetricLabels,
        amount: int = 1,
    ) -> None:
        if amount < 1:
            raise ValueError("counter increments must be positive")
        key = (name, _safe_labels(name, labels))
        self.counters[key] = self.counters.get(key, 0) + amount

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: MetricLabels,
    ) -> None:
        if value < 0:
            raise ValueError("metric observations must be non-negative")
        self.observations.append((name, value, _safe_labels(name, labels)))
