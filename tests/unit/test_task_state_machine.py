from __future__ import annotations

import pytest

from ninjatech_deployment_lab.tasks.domain import (
    InvalidTaskTransitionError,
    TaskCommand,
    TaskStatus,
    apply_task_command,
)

EXPECTED_TRANSITIONS: dict[
    tuple[TaskStatus, TaskCommand],
    TaskStatus | None,
] = {(status, command): None for status in TaskStatus for command in TaskCommand}
EXPECTED_TRANSITIONS.update(
    {
        (TaskStatus.PENDING_APPROVAL, TaskCommand.APPROVE): TaskStatus.APPROVED,
        (TaskStatus.APPROVED, TaskCommand.APPROVE): TaskStatus.APPROVED,
        (TaskStatus.PENDING_APPROVAL, TaskCommand.CANCEL): TaskStatus.CANCELLED,
        (TaskStatus.APPROVED, TaskCommand.CANCEL): TaskStatus.CANCELLED,
        (TaskStatus.CANCELLED, TaskCommand.CANCEL): TaskStatus.CANCELLED,
        (TaskStatus.APPROVED, TaskCommand.START): TaskStatus.RUNNING,
        (TaskStatus.RUNNING, TaskCommand.SUCCEED): TaskStatus.SUCCEEDED,
        (TaskStatus.RUNNING, TaskCommand.FAIL): TaskStatus.FAILED,
    }
)


@pytest.mark.parametrize(
    ("current_status", "command", "expected_status"),
    [
        (current_status, command, EXPECTED_TRANSITIONS[(current_status, command)])
        for current_status in TaskStatus
        for command in TaskCommand
    ],
)
def test_complete_task_transition_matrix(
    current_status: TaskStatus,
    command: TaskCommand,
    expected_status: TaskStatus | None,
) -> None:
    if expected_status is None:
        with pytest.raises(InvalidTaskTransitionError):
            apply_task_command(current_status, command)
        return

    assert apply_task_command(current_status, command) == expected_status


def test_repeated_approve_is_idempotent() -> None:
    assert apply_task_command(TaskStatus.APPROVED, TaskCommand.APPROVE) == TaskStatus.APPROVED


def test_repeated_cancel_is_idempotent() -> None:
    assert apply_task_command(TaskStatus.CANCELLED, TaskCommand.CANCEL) == TaskStatus.CANCELLED
