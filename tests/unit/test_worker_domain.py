from __future__ import annotations

import math

import pytest

from ninjatech_deployment_lab.worker.domain import (
    HandlerContractError,
    RetryPolicy,
    validate_handler_result,
)


@pytest.mark.parametrize(
    ("random_value", "expected"),
    [(0.0, 2.0), (0.5, 3.0), (1.0, 4.0)],
)
def test_equal_jitter_is_deterministic_and_bounded(
    random_value: float,
    expected: float,
) -> None:
    policy = RetryPolicy(2.0, 60.0, lambda: random_value)

    assert policy.delay_seconds(2) == expected


def test_backoff_caps_exponential_growth() -> None:
    policy = RetryPolicy(2.0, 10.0, lambda: 1.0)

    assert policy.delay_seconds(20) == 10.0


def test_backoff_rejects_invalid_random_source() -> None:
    policy = RetryPolicy(1.0, 10.0, lambda: 1.1)

    with pytest.raises(ValueError):
        policy.delay_seconds(1)


def test_valid_result_is_returned() -> None:
    result = {"ok": True, "nested": {"items": [1, 2, 3]}}

    assert validate_handler_result(result, maximum_bytes=1024) == result


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_results_are_rejected_recursively(value: float) -> None:
    with pytest.raises(HandlerContractError):
        validate_handler_result(
            {"nested": {"invalid": value}},
            maximum_bytes=1024,
        )


def test_oversized_result_is_rejected() -> None:
    with pytest.raises(HandlerContractError):
        validate_handler_result({"value": "x" * 100}, maximum_bytes=20)


@pytest.mark.parametrize("result", [None, [], "value", {"value": object()}])
def test_non_object_or_non_serializable_results_are_rejected(result: object) -> None:
    with pytest.raises(HandlerContractError):
        validate_handler_result(result, maximum_bytes=1024)
