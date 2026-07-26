from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ninjatech_deployment_lab.database import Base
from ninjatech_deployment_lab.tasks.domain import TaskStatus
from ninjatech_deployment_lab.tasks.schemas import JsonValue


def utc_now() -> datetime:
    """Return an aware UTC timestamp for explicit lifecycle timestamps."""
    return datetime.now(UTC)


class Task(Base):
    """Persistent task and its current lifecycle state."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        CheckConstraint(
            "status IN "
            "('pending_approval', 'approved', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_tasks_idempotency_key_length",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_tasks_request_fingerprint",
        ),
        CheckConstraint(
            "task_type ~ '^[a-z][a-z0-9_]{0,99}$'",
            name="ck_tasks_task_type",
        ),
        CheckConstraint(
            "jsonb_typeof(task_input) = 'object'",
            name="ck_tasks_input_object",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    task_input: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [status.value for status in enum],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
