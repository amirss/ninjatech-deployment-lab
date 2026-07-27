from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class TaskStatus(StrEnum):
    """Persisted lifecycle states for a task."""

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCommand(StrEnum):
    """Commands understood by the task state machine."""

    APPROVE = "approve"
    CANCEL = "cancel"
    START = "start"
    SUCCEED = "succeed"
    FAIL = "fail"
    RETRY = "retry"
    FINALIZE_CANCELLATION = "finalize_cancellation"


class AttemptStatus(StrEnum):
    """Durable outcomes for one execution attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LEASE_EXPIRED = "lease_expired"


@dataclass(frozen=True, slots=True)
class CancellationDecision:
    """Centralized state and metadata decision for a cancel command."""

    new_status: TaskStatus
    request_cancellation: bool


class TaskNotFoundError(Exception):
    """Raised when a task ID does not exist."""

    def __init__(self, task_id: UUID) -> None:
        self.task_id = task_id
        super().__init__(f"Task {task_id} was not found")


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused for different request content."""

    def __init__(self) -> None:
        super().__init__("Idempotency-Key was already used for a different request")


class InvalidTaskTransitionError(Exception):
    """Raised when a command is not valid for the current task status."""

    def __init__(self, current_status: TaskStatus, command: TaskCommand) -> None:
        self.current_status = current_status
        self.command = command
        super().__init__(
            f"Command {command.value} is not allowed from status {current_status.value}"
        )


_TRANSITIONS: dict[TaskCommand, dict[TaskStatus, TaskStatus]] = {
    TaskCommand.APPROVE: {
        TaskStatus.PENDING_APPROVAL: TaskStatus.APPROVED,
        TaskStatus.APPROVED: TaskStatus.APPROVED,
    },
    TaskCommand.CANCEL: {
        TaskStatus.PENDING_APPROVAL: TaskStatus.CANCELLED,
        TaskStatus.APPROVED: TaskStatus.CANCELLED,
        TaskStatus.RUNNING: TaskStatus.RUNNING,
        TaskStatus.CANCELLED: TaskStatus.CANCELLED,
    },
    TaskCommand.START: {
        TaskStatus.APPROVED: TaskStatus.RUNNING,
    },
    TaskCommand.SUCCEED: {
        TaskStatus.RUNNING: TaskStatus.SUCCEEDED,
    },
    TaskCommand.FAIL: {
        TaskStatus.RUNNING: TaskStatus.FAILED,
    },
    TaskCommand.RETRY: {
        TaskStatus.RUNNING: TaskStatus.APPROVED,
    },
    TaskCommand.FINALIZE_CANCELLATION: {
        TaskStatus.RUNNING: TaskStatus.CANCELLED,
    },
}


def apply_task_command(current_status: TaskStatus, command: TaskCommand) -> TaskStatus:
    """Return the command result or reject the prohibited transition."""
    try:
        return _TRANSITIONS[command][current_status]
    except KeyError:
        raise InvalidTaskTransitionError(current_status, command) from None


def decide_cancellation(
    current_status: TaskStatus,
    *,
    already_requested: bool,
) -> CancellationDecision:
    """Return the centralized status and cancellation-marker decision."""
    new_status = apply_task_command(current_status, TaskCommand.CANCEL)
    return CancellationDecision(
        new_status=new_status,
        request_cancellation=(
            current_status
            in {
                TaskStatus.PENDING_APPROVAL,
                TaskStatus.APPROVED,
                TaskStatus.RUNNING,
            }
            and not already_requested
        ),
    )
