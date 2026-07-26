from __future__ import annotations

import math

import pytest
from pydantic import TypeAdapter, ValidationError

from ninjatech_deployment_lab.tasks.schemas import (
    IdempotencyKey,
    TaskCreateRequest,
)

idempotency_key_adapter = TypeAdapter(IdempotencyKey)


@pytest.mark.parametrize(
    "invalid_key",
    [
        "",
        "a" * 256,
        "contains space",
        "contains\ttab",
        "contains\nnewline",
        "non-ascii-\N{SNOWMAN}",
    ],
)
def test_invalid_idempotency_keys_are_rejected(invalid_key: str) -> None:
    with pytest.raises(ValidationError):
        idempotency_key_adapter.validate_python(invalid_key)


def test_visible_ascii_idempotency_key_is_accepted() -> None:
    key = "task:create-123_ABC.example"

    assert idempotency_key_adapter.validate_python(key) == key


def test_input_must_be_json_object() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate(
            {"task_type": "code_change", "input": ["not", "an", "object"]}
        )


def test_unknown_top_level_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate(
            {
                "task_type": "code_change",
                "input": {},
                "unexpected": "field",
            }
        )


@pytest.mark.parametrize(
    "invalid_task_type",
    ["", "CodeChange", "CODE_CHANGE", "code-change", "_code_change", "code change"],
)
def test_malformed_task_types_are_rejected(invalid_task_type: str) -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({"task_type": invalid_task_type, "input": {}})


@pytest.mark.parametrize(
    "non_finite_value",
    [math.nan, math.inf, -math.inf],
)
def test_non_finite_numbers_are_rejected_recursively(
    non_finite_value: float,
) -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate(
            {
                "task_type": "code_change",
                "input": {
                    "nested": {
                        "items": [
                            {"value": non_finite_value},
                        ]
                    }
                },
            }
        )
