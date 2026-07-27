from __future__ import annotations

import copy
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ninjatech_deployment_lab.tasks.domain import (
    AttemptStatus,
    TaskCommand,
    TaskStatus,
    apply_task_command,
)
from ninjatech_deployment_lab.tasks.model import Task, TaskAttempt
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import (
    ClaimedTask,
    ExecutionInvariantError,
    FenceResult,
    HeartbeatResult,
    RecoveryResult,
    hash_lease_token,
)

BackoffCalculator = Callable[[int], float]


class WorkerRepository:
    """Short, token-fenced PostgreSQL transactions for worker coordination."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def claim_one(
        self,
        *,
        supported_task_types: tuple[str, ...],
        worker_id: str,
        lease_duration_seconds: float,
    ) -> ClaimedTask | None:
        """Claim one due supported task and commit its attempt before returning."""
        if not supported_task_types:
            return None

        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_lease_token(raw_token)
        attempt_id = uuid4()
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.APPROVED,
                        Task.available_at <= func.clock_timestamp(),
                        Task.attempt_count < Task.max_attempts,
                        Task.task_type.in_(supported_task_types),
                    )
                    .order_by(Task.available_at, Task.created_at, Task.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                task = result.scalar_one_or_none()
                if task is None:
                    return None

                apply_task_command(task.status, TaskCommand.START)
                database_now = await self._database_now(session)
                attempt_number = task.attempt_count + 1
                task.status = TaskStatus.RUNNING
                task.worker_id = worker_id
                task.lease_token_hash = token_hash
                task.lease_expires_at = database_now + timedelta(seconds=lease_duration_seconds)
                task.last_heartbeat_at = database_now
                task.available_at = None
                task.attempt_count = attempt_number
                task.updated_at = self._next_updated_at(task.updated_at, database_now)
                task.result = None

                session.add(
                    TaskAttempt(
                        id=attempt_id,
                        task_id=task.id,
                        attempt_number=attempt_number,
                        worker_id=worker_id,
                        lease_token_hash=token_hash,
                        status=AttemptStatus.RUNNING,
                        started_at=database_now,
                        last_heartbeat_at=database_now,
                    )
                )
                await session.flush()

                claimed = ClaimedTask(
                    task_id=task.id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    max_attempts=task.max_attempts,
                    worker_id=worker_id,
                    lease_token_hash=token_hash,
                    task_type=task.task_type,
                    task_input=copy.deepcopy(task.task_input),
                )
        return claimed

    async def heartbeat(
        self,
        claim: ClaimedTask,
        *,
        lease_duration_seconds: float,
    ) -> HeartbeatResult:
        """Extend one active lease and its exact attempt atomically."""
        async with self.session_factory() as session:
            async with session.begin():
                database_now = await self._database_now(session)
                task_result = await session.execute(
                    update(Task)
                    .where(*self._active_fence(claim, database_now))
                    .values(
                        last_heartbeat_at=database_now,
                        lease_expires_at=case(
                            (
                                Task.cancellation_requested_at.is_(None),
                                database_now + timedelta(seconds=lease_duration_seconds),
                            ),
                            else_=Task.lease_expires_at,
                        ),
                    )
                    .returning(
                        Task.cancellation_requested_at,
                        Task.last_heartbeat_at,
                    )
                )
                heartbeat_row = task_result.one_or_none()
                if heartbeat_row is None:
                    raise ExecutionInvariantError("active task fence did not match")

                attempt_result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(TaskAttempt)
                        .where(
                            TaskAttempt.task_id == claim.task_id,
                            TaskAttempt.attempt_number == claim.attempt_number,
                            TaskAttempt.lease_token_hash == claim.lease_token_hash,
                            TaskAttempt.status == AttemptStatus.RUNNING,
                        )
                        .values(last_heartbeat_at=heartbeat_row.last_heartbeat_at)
                    ),
                )
                self._require_one_row(attempt_result, "heartbeat attempt")
                return (
                    HeartbeatResult.CANCELLATION_REQUESTED
                    if heartbeat_row.cancellation_requested_at is not None
                    else HeartbeatResult.ACTIVE
                )

    async def finalize_success(
        self,
        claim: ClaimedTask,
        result: dict[str, JsonValue],
    ) -> FenceResult:
        """Persist a result and successful attempt under the active fence."""
        return await self._finalize(
            claim,
            required_cancellation=False,
            task_values={
                "status": TaskStatus.SUCCEEDED,
                "result": result,
                "available_at": None,
                "last_error_code": None,
                "last_error_summary": None,
            },
            attempt_values={
                "status": AttemptStatus.SUCCEEDED,
                "terminal_reason": "completed",
            },
        )

    async def schedule_retry(
        self,
        claim: ClaimedTask,
        *,
        delay_seconds: float,
        error_code: str,
        error_summary: str,
        terminal_reason: str,
    ) -> FenceResult:
        """Return a running task to the approved queue under the active fence."""
        apply_task_command(TaskStatus.RUNNING, TaskCommand.RETRY)
        return await self._finalize(
            claim,
            required_cancellation=False,
            task_values={
                "status": TaskStatus.APPROVED,
                "available_at": func.clock_timestamp() + timedelta(seconds=delay_seconds),
                "result": None,
                "last_error_code": error_code,
                "last_error_summary": error_summary,
            },
            attempt_values={
                "status": AttemptStatus.RETRY_SCHEDULED,
                "error_code": error_code,
                "error_summary": error_summary,
                "terminal_reason": terminal_reason,
            },
        )

    async def finalize_failure(
        self,
        claim: ClaimedTask,
        *,
        error_code: str,
        error_summary: str,
        terminal_reason: str,
    ) -> FenceResult:
        """Permanently fail a task and its active attempt atomically."""
        apply_task_command(TaskStatus.RUNNING, TaskCommand.FAIL)
        return await self._finalize(
            claim,
            required_cancellation=False,
            task_values={
                "status": TaskStatus.FAILED,
                "available_at": None,
                "result": None,
                "last_error_code": error_code,
                "last_error_summary": error_summary,
            },
            attempt_values={
                "status": AttemptStatus.FAILED,
                "error_code": error_code,
                "error_summary": error_summary,
                "terminal_reason": terminal_reason,
            },
        )

    async def finalize_cancellation(self, claim: ClaimedTask) -> FenceResult:
        """Acknowledge a durable customer cancellation under the active fence."""
        apply_task_command(TaskStatus.RUNNING, TaskCommand.FINALIZE_CANCELLATION)
        return await self._finalize(
            claim,
            required_cancellation=True,
            task_values={
                "status": TaskStatus.CANCELLED,
                "available_at": None,
                "result": None,
                "last_error_code": None,
                "last_error_summary": None,
            },
            attempt_values={
                "status": AttemptStatus.CANCELLED,
                "terminal_reason": "customer_cancellation",
            },
        )

    async def recover_one_expired(
        self,
        *,
        backoff_seconds: BackoffCalculator,
    ) -> RecoveryResult | None:
        """Close one expired attempt and atomically choose its task outcome."""
        async with self.session_factory() as session:
            async with session.begin():
                database_now = await self._database_now(session)
                result = await session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.RUNNING,
                        Task.lease_expires_at <= database_now,
                    )
                    .order_by(Task.lease_expires_at, Task.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                task = result.scalar_one_or_none()
                if task is None:
                    return None
                if (
                    task.worker_id is None
                    or task.lease_token_hash is None
                    or task.attempt_count <= 0
                ):
                    raise ExecutionInvariantError("expired task has incomplete lease state")
                task_id = task.id
                attempt_number = task.attempt_count
                previous_worker_id = task.worker_id
                token_hash = task.lease_token_hash
                current_updated_at = task.updated_at

                attempt_result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(TaskAttempt)
                        .where(
                            TaskAttempt.task_id == task_id,
                            TaskAttempt.attempt_number == attempt_number,
                            TaskAttempt.lease_token_hash == token_hash,
                            TaskAttempt.status == AttemptStatus.RUNNING,
                        )
                        .values(
                            status=AttemptStatus.LEASE_EXPIRED,
                            finished_at=database_now,
                            error_code="lease_expired",
                            error_summary="Worker lease expired before completion",
                            terminal_reason=(
                                "customer_cancellation"
                                if task.cancellation_requested_at is not None
                                else "lease_expired"
                            ),
                        )
                    ),
                )
                self._require_one_row(attempt_result, "expired attempt")

                if task.cancellation_requested_at is not None:
                    apply_task_command(
                        TaskStatus.RUNNING,
                        TaskCommand.FINALIZE_CANCELLATION,
                    )
                    new_status = TaskStatus.CANCELLED
                    next_attempt_at = None
                    terminal_reason = "customer_cancellation"
                    error_code = None
                    error_summary = None
                elif attempt_number >= task.max_attempts:
                    apply_task_command(TaskStatus.RUNNING, TaskCommand.FAIL)
                    new_status = TaskStatus.FAILED
                    next_attempt_at = None
                    terminal_reason = "max_attempts_exhausted"
                    error_code = "lease_expired"
                    error_summary = "Maximum attempts exhausted after lease expiration"
                else:
                    apply_task_command(TaskStatus.RUNNING, TaskCommand.RETRY)
                    new_status = TaskStatus.APPROVED
                    next_attempt_at = database_now + timedelta(
                        seconds=backoff_seconds(attempt_number)
                    )
                    terminal_reason = "lease_expired"
                    error_code = "lease_expired"
                    error_summary = "Worker lease expired; retry scheduled"

                task_result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(Task)
                        .where(
                            Task.id == task_id,
                            Task.status == TaskStatus.RUNNING,
                            Task.attempt_count == attempt_number,
                            Task.lease_token_hash == token_hash,
                            Task.lease_expires_at <= database_now,
                        )
                        .values(
                            status=new_status,
                            available_at=next_attempt_at,
                            worker_id=None,
                            lease_token_hash=None,
                            lease_expires_at=None,
                            last_heartbeat_at=None,
                            result=None,
                            last_error_code=error_code,
                            last_error_summary=error_summary,
                            updated_at=self._next_updated_at(
                                current_updated_at,
                                database_now,
                            ),
                        )
                    ),
                )
                self._require_one_row(task_result, "expired task")

                attempt_id = (
                    await session.execute(
                        select(TaskAttempt.id).where(
                            TaskAttempt.task_id == task_id,
                            TaskAttempt.attempt_number == attempt_number,
                            TaskAttempt.lease_token_hash == token_hash,
                        )
                    )
                ).scalar_one()
                recovery = RecoveryResult(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    previous_worker_id=previous_worker_id,
                    new_status=new_status.value,
                    next_attempt_at=(
                        next_attempt_at.isoformat() if next_attempt_at is not None else None
                    ),
                    terminal_reason=terminal_reason,
                )
        return recovery

    async def _finalize(
        self,
        claim: ClaimedTask,
        *,
        required_cancellation: bool,
        task_values: dict[str, Any],
        attempt_values: dict[str, Any],
    ) -> FenceResult:
        async with self.session_factory() as session:
            async with session.begin():
                database_now = await self._database_now(session)
                cancellation_condition = (
                    Task.cancellation_requested_at.is_not(None)
                    if required_cancellation
                    else Task.cancellation_requested_at.is_(None)
                )
                task_result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(Task)
                        .where(
                            *self._active_fence(claim, database_now),
                            cancellation_condition,
                        )
                        .values(
                            **task_values,
                            worker_id=None,
                            lease_token_hash=None,
                            lease_expires_at=None,
                            last_heartbeat_at=None,
                            updated_at=func.greatest(
                                database_now,
                                Task.updated_at + timedelta(microseconds=1),
                            ),
                        )
                    ),
                )
                if task_result.rowcount == 0:
                    return await self._classify_failed_fence(
                        session,
                        claim,
                        database_now,
                    )
                self._require_one_row(task_result, "finalized task")

                attempt_result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(TaskAttempt)
                        .where(
                            TaskAttempt.task_id == claim.task_id,
                            TaskAttempt.attempt_number == claim.attempt_number,
                            TaskAttempt.lease_token_hash == claim.lease_token_hash,
                            TaskAttempt.status == AttemptStatus.RUNNING,
                        )
                        .values(**attempt_values, finished_at=database_now)
                    ),
                )
                self._require_one_row(attempt_result, "finalized attempt")
                return FenceResult.APPLIED

    async def _classify_failed_fence(
        self,
        session: AsyncSession,
        claim: ClaimedTask,
        database_now: datetime,
    ) -> FenceResult:
        row = (
            await session.execute(
                select(Task.id, Task.cancellation_requested_at).where(
                    *self._active_fence(claim, database_now)
                )
            )
        ).one_or_none()
        return (
            FenceResult.CANCELLATION_REQUESTED
            if row is not None and row.cancellation_requested_at is not None
            else FenceResult.STALE
        )

    @staticmethod
    async def _database_now(session: AsyncSession) -> datetime:
        return cast(
            datetime,
            (await session.execute(select(func.clock_timestamp()))).scalar_one(),
        )

    @staticmethod
    def _active_fence(
        claim: ClaimedTask,
        database_now: datetime,
    ) -> tuple[Any, ...]:
        return (
            Task.id == claim.task_id,
            Task.status == TaskStatus.RUNNING,
            Task.worker_id == claim.worker_id,
            Task.attempt_count == claim.attempt_number,
            Task.lease_token_hash == claim.lease_token_hash,
            Task.lease_expires_at > database_now,
        )

    @staticmethod
    def _next_updated_at(current: datetime, database_now: datetime) -> datetime:
        return max(database_now, current + timedelta(microseconds=1))

    @staticmethod
    def _require_one_row(result: CursorResult[Any], operation: str) -> None:
        if result.rowcount != 1:
            raise ExecutionInvariantError(
                f"{operation} expected one row but affected {result.rowcount}"
            )
