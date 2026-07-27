from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from ninjatech_deployment_lab.tasks.schemas import JsonValue

RandomSource = Callable[[], float]


class RetryableTaskError(Exception):
    """A sanitized handler outcome that may be attempted again."""

    def __init__(
        self,
        error_code: str = "retryable_task_error",
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Task handler reported a retryable failure")


class PermanentTaskError(Exception):
    """A sanitized handler outcome that must not be attempted again."""

    def __init__(self, error_code: str = "permanent_task_error") -> None:
        self.error_code = error_code
        super().__init__("Task handler reported a permanent failure")


class TaskCancelled(Exception):
    """Raised by handler context after a customer cancellation request."""


class TaskExecutionTimeout(Exception):
    """Internal timeout classification for a cooperative handler."""


class OwnershipLostError(Exception):
    """Raised when PostgreSQL cannot confirm the active execution fence."""


class ExecutionInvariantError(RuntimeError):
    """Raised to roll back a task/attempt transaction that became inconsistent."""


class HandlerContractError(PermanentTaskError):
    """Raised when a handler returns an unsafe or invalid result."""

    def __init__(self) -> None:
        super().__init__("handler_contract_error")


class ExecutionStopCause(StrEnum):
    """Distinct reasons for cancelling a local asyncio handler task."""

    CUSTOMER_CANCELLATION = "customer_cancellation"
    TIMEOUT = "timeout"
    WORKER_SHUTDOWN = "worker_shutdown"
    OWNERSHIP_LOSS = "ownership_loss"


class FenceResult(StrEnum):
    """Outcome of a token-qualified worker write."""

    APPLIED = "applied"
    CANCELLATION_REQUESTED = "cancellation_requested"
    STALE = "stale"


class HeartbeatResult(StrEnum):
    """Outcome of an active execution heartbeat."""

    ACTIVE = "active"
    CANCELLATION_REQUESTED = "cancellation_requested"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded equal-jitter retry policy."""

    base_seconds: float
    cap_seconds: float
    random_source: RandomSource

    def delay_seconds(self, attempt_number: int) -> float:
        """Return a positive bounded delay for the completed attempt."""
        ceiling: float = min(
            self.cap_seconds,
            self.base_seconds * (2 ** (attempt_number - 1)),
        )
        random_value = self.random_source()
        if not 0.0 <= random_value <= 1.0:
            raise ValueError("random source must return a value between zero and one")
        return float((ceiling / 2) + ((ceiling / 2) * random_value))


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """Immutable execution data returned after a committed claim."""

    task_id: UUID
    attempt_id: UUID
    attempt_number: int
    max_attempts: int
    worker_id: str
    lease_token_hash: str
    task_type: str
    task_input: dict[str, JsonValue]

    @property
    def execution_fence(self) -> ExecutionFence:
        return ExecutionFence(
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            attempt_number=self.attempt_number,
            worker_id=self.worker_id,
            lease_token_hash=self.lease_token_hash,
        )


@dataclass(frozen=True, slots=True)
class ExecutionFence:
    """Database-verifiable ownership capability; never exposed or logged."""

    task_id: UUID
    attempt_id: UUID
    attempt_number: int
    worker_id: str
    lease_token_hash: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Evidence emitted after one expired lease is reconciled."""

    task_id: UUID
    attempt_id: UUID
    attempt_number: int
    previous_worker_id: str
    new_status: str
    next_attempt_at: str | None
    terminal_reason: str


def hash_lease_token(raw_token: str) -> str:
    """Hash a process-memory-only execution capability for database matching."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _contains_non_finite(value: JsonValue) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, list):
        return any(_contains_non_finite(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_non_finite(item) for item in value.values())
    return False


def validate_handler_result(
    result: Any,
    *,
    maximum_bytes: int,
) -> dict[str, JsonValue]:
    """Validate a public-safe JSON-object result without logging its contents."""
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        raise HandlerContractError
    typed_result = result
    if _contains_non_finite(typed_result):
        raise HandlerContractError
    try:
        serialized = json.dumps(
            typed_result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError):
        raise HandlerContractError from None
    if len(serialized) > maximum_bytes:
        raise HandlerContractError
    return typed_result
