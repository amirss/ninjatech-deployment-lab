from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from ninjatech_deployment_lab.tasks.domain import TaskStatus

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

IdempotencyKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=255,
        pattern=r"^[\x21-\x7E]+$",
    ),
]


def reject_non_finite_numbers(value: JsonValue) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        msg = "input must not contain NaN or Infinity"
        raise ValueError(msg)
    if isinstance(value, list):
        for item in value:
            reject_non_finite_numbers(item)
    if isinstance(value, dict):
        for item in value.values():
            reject_non_finite_numbers(item)


class TaskCreateRequest(BaseModel):
    """Validated request used to create an idempotent task."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    task_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    input: dict[str, JsonValue]

    @field_validator("input")
    @classmethod
    def reject_non_finite_numbers(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Reject non-standard JSON numbers at any nesting depth."""
        reject_non_finite_numbers(value)
        return value


class TaskResponse(BaseModel):
    """Public task representation without internal idempotency metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_type: str
    input: dict[str, JsonValue] = Field(validation_alias="task_input")
    status: TaskStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime | None
    cancellation_requested_at: datetime | None
    result: dict[str, JsonValue] | None
    last_error_code: str | None
    last_error_summary: str | None
    created_at: datetime
    updated_at: datetime


class ErrorDetail(BaseModel):
    """Stable machine-readable API error details."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """Public API error envelope."""

    error: ErrorDetail
