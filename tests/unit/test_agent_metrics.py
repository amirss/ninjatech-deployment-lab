from __future__ import annotations

from typing import cast

import pytest

from ninjatech_deployment_lab.integrations.metrics import (
    InMemoryMetricsSink,
    MetricLabel,
    MetricLabels,
    MetricName,
    MetricOperation,
    MetricOutcome,
    MetricProposalOutcome,
    MetricProvider,
    MetricTool,
)


def test_agent_metrics_accept_only_low_cardinality_enums() -> None:
    sink = InMemoryMetricsSink()
    sink.increment(
        MetricName.MODEL_REQUEST_COUNT,
        labels={
            MetricLabel.PROVIDER: MetricProvider.RECORDED,
            MetricLabel.OPERATION: MetricOperation.MODEL_COMPLETE,
            MetricLabel.OUTCOME: MetricOutcome.SUCCESS,
        },
    )
    sink.increment(
        MetricName.REPOSITORY_TOOL_CALL_COUNT,
        labels={
            MetricLabel.TOOL: MetricTool.READ_FILES,
            MetricLabel.OUTCOME: MetricOutcome.SUCCESS,
        },
    )
    sink.increment(
        MetricName.PROPOSAL_OUTCOME_COUNT,
        labels={MetricLabel.PROPOSAL_OUTCOME: MetricProposalOutcome.PROPOSED},
    )
    assert len(sink.counters) == 3


def test_agent_metrics_reject_paths_ids_urls_and_free_form_values() -> None:
    sink = InMemoryMetricsSink()
    unsafe_outcome = cast(
        MetricLabels,
        {
            MetricLabel.PROVIDER: MetricProvider.RECORDED,
            MetricLabel.OPERATION: MetricOperation.MODEL_COMPLETE,
            MetricLabel.OUTCOME: "repo/customer/service",
        },
    )
    with pytest.raises(ValueError):
        sink.increment(
            MetricName.MODEL_REQUEST_COUNT,
            labels=unsafe_outcome,
        )
    unsafe_tool = cast(
        MetricLabels,
        {
            MetricLabel.TOOL: "src/private.py",
            MetricLabel.OUTCOME: MetricOutcome.SUCCESS,
        },
    )
    with pytest.raises(ValueError):
        sink.increment(
            MetricName.REPOSITORY_TOOL_CALL_COUNT,
            labels=unsafe_tool,
        )
