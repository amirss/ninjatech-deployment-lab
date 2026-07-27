from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ninjatech_deployment_lab.tasks.domain import (
    IdempotencyConflictError,
    TaskCommand,
    TaskNotFoundError,
    TaskStatus,
    apply_task_command,
    decide_cancellation,
)
from ninjatech_deployment_lab.tasks.model import Task
from ninjatech_deployment_lab.tasks.repository import TaskRepository
from ninjatech_deployment_lab.tasks.schemas import JsonValue

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreateTaskResult:
    """A task plus whether this request inserted it."""

    task: Task
    created: bool


def task_request_fingerprint(
    task_type: str,
    task_input: dict[str, JsonValue],
) -> str:
    """Hash deterministic canonical JSON for idempotency comparison."""
    canonical_request = json.dumps(
        {"input": task_input, "task_type": task_type},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical_request).hexdigest()


def idempotency_key_log_hash(idempotency_key: str) -> str:
    """Return a short irreversible identifier suitable only for correlation logs."""
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]


def next_updated_at(current_updated_at: datetime) -> datetime:
    """Return a strictly increasing timestamp even under coarse or regressing clocks."""
    now = datetime.now(UTC)
    if now <= current_updated_at:
        return current_updated_at + timedelta(microseconds=1)
    return now


def log_task_event(
    *,
    event: str,
    task: Task,
    previous_status: TaskStatus | None,
    new_status: TaskStatus,
    idempotency_key: str | None = None,
) -> None:
    """Log lifecycle metadata without task input or raw idempotency material."""
    extra: dict[str, str | None] = {
        "event": event,
        "task_id": str(task.id),
        "task_type": task.task_type,
        "previous_status": previous_status.value if previous_status is not None else None,
        "new_status": new_status.value,
    }
    if idempotency_key is not None:
        extra["idempotency_key_hash"] = idempotency_key_log_hash(idempotency_key)
    logger.info("task_lifecycle", extra=extra)


class TaskService:
    """Coordinate task transactions, state rules, and safe lifecycle logging."""

    def __init__(
        self,
        session: AsyncSession,
        repository: TaskRepository | None = None,
        *,
        default_max_attempts: int = 3,
    ) -> None:
        self.session = session
        self.repository = repository or TaskRepository()
        self.default_max_attempts = default_max_attempts

    async def create_task(
        self,
        *,
        idempotency_key: str,
        task_type: str,
        task_input: dict[str, JsonValue],
    ) -> CreateTaskResult:
        """Create once or replay the task bound to the same canonical request."""
        fingerprint = task_request_fingerprint(task_type, task_input)
        timestamp = datetime.now(UTC)

        async with self.session.begin():
            task = await self.repository.insert_if_absent(
                self.session,
                task_id=uuid4(),
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                task_type=task_type,
                task_input=task_input,
                timestamp=timestamp,
                max_attempts=self.default_max_attempts,
            )
            if task is not None:
                result = CreateTaskResult(task=task, created=True)
            else:
                existing_task = await self.repository.get_by_idempotency_key(
                    self.session,
                    idempotency_key,
                )
                if existing_task is None:
                    msg = "Idempotency conflict did not resolve to a persisted task"
                    raise RuntimeError(msg)
                if existing_task.request_fingerprint != fingerprint:
                    logger.warning(
                        "task_idempotency_conflict",
                        extra={
                            "event": "task_idempotency_conflict",
                            "task_id": str(existing_task.id),
                            "task_type": existing_task.task_type,
                            "idempotency_key_hash": idempotency_key_log_hash(idempotency_key),
                        },
                    )
                    raise IdempotencyConflictError
                result = CreateTaskResult(task=existing_task, created=False)

        log_task_event(
            event="task_created" if result.created else "task_create_replayed",
            task=result.task,
            previous_status=None if result.created else result.task.status,
            new_status=result.task.status,
            idempotency_key=idempotency_key,
        )
        return result

    async def get_task(self, task_id: UUID) -> Task:
        """Return one task or raise a domain not-found error."""
        task = await self.repository.get_by_id(self.session, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    async def apply_command(self, task_id: UUID, command: TaskCommand) -> Task:
        """Serialize, validate, and persist one state-machine command."""
        async with self.session.begin():
            task = await self.repository.get_by_id_for_update(self.session, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)

            previous_status = task.status
            new_status = apply_task_command(previous_status, command)
            if new_status != previous_status:
                database_now = (
                    await self.session.execute(select(func.clock_timestamp()))
                ).scalar_one()
                task.status = new_status
                task.updated_at = max(
                    database_now,
                    task.updated_at + timedelta(microseconds=1),
                )
                if command is TaskCommand.APPROVE:
                    task.available_at = database_now
                await self.session.flush()

        log_task_event(
            event=f"task_{command.value}",
            task=task,
            previous_status=previous_status,
            new_status=new_status,
        )
        return task

    async def approve_task(self, task_id: UUID) -> Task:
        """Approve a pending task or replay an existing approval."""
        return await self.apply_command(task_id, TaskCommand.APPROVE)

    async def cancel_task(self, task_id: UUID) -> Task:
        """Cancel immediately or record a cooperative running cancellation."""
        async with self.session.begin():
            task = await self.repository.get_by_id_for_update(self.session, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)

            previous_status = task.status
            decision = decide_cancellation(
                previous_status,
                already_requested=task.cancellation_requested_at is not None,
            )
            changed = decision.new_status != previous_status or decision.request_cancellation
            if changed:
                database_now = (
                    await self.session.execute(select(func.clock_timestamp()))
                ).scalar_one()
                task.status = decision.new_status
                if decision.request_cancellation:
                    task.cancellation_requested_at = database_now
                if decision.new_status is TaskStatus.CANCELLED:
                    task.available_at = None
                task.updated_at = max(
                    database_now,
                    task.updated_at + timedelta(microseconds=1),
                )
                await self.session.flush()

        log_task_event(
            event=(
                "task_cancellation_requested"
                if previous_status is TaskStatus.RUNNING
                else "task_cancel"
            ),
            task=task,
            previous_status=previous_status,
            new_status=decision.new_status,
        )
        return task
