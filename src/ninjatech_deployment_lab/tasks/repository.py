from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ninjatech_deployment_lab.tasks.domain import TaskStatus
from ninjatech_deployment_lab.tasks.model import Task
from ninjatech_deployment_lab.tasks.schemas import JsonValue


class TaskRepository:
    """Task-specific SQLAlchemy operations without a generic repository layer."""

    async def insert_if_absent(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        task_type: str,
        task_input: dict[str, JsonValue],
        timestamp: datetime,
    ) -> Task | None:
        """Atomically insert a task or return none when its idempotency key exists."""
        statement = (
            insert(Task)
            .values(
                id=task_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                task_type=task_type,
                task_input=task_input,
                status=TaskStatus.PENDING_APPROVAL,
                created_at=timestamp,
                updated_at=timestamp,
            )
            .on_conflict_do_nothing(constraint="uq_tasks_idempotency_key")
            .returning(Task)
        )
        result = await session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(
        self,
        session: AsyncSession,
        idempotency_key: str,
    ) -> Task | None:
        """Load a task by its globally unique idempotency key."""
        result = await session.execute(select(Task).where(Task.idempotency_key == idempotency_key))
        return result.scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, task_id: UUID) -> Task | None:
        """Load the latest committed task state without locking it."""
        return await session.get(Task, task_id)

    async def get_by_id_for_update(
        self,
        session: AsyncSession,
        task_id: UUID,
    ) -> Task | None:
        """Lock one task row so a transition sees and updates a serialized state."""
        result = await session.execute(select(Task).where(Task.id == task_id).with_for_update())
        return result.scalar_one_or_none()
