from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ninjatech_deployment_lab.database import Base
from ninjatech_deployment_lab.tasks.domain import AttemptStatus, TaskStatus
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
        CheckConstraint("attempt_count >= 0", name="ck_tasks_attempt_count_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="ck_tasks_max_attempts_positive"),
        CheckConstraint(
            "attempt_count <= max_attempts",
            name="ck_tasks_attempt_count_within_max",
        ),
        CheckConstraint(
            "status <> 'approved' OR available_at IS NOT NULL",
            name="ck_tasks_approved_available",
        ),
        CheckConstraint(
            "("
            "status = 'running' AND worker_id IS NOT NULL "
            "AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND last_heartbeat_at IS NOT NULL AND attempt_count > 0"
            ") OR ("
            "status <> 'running' AND worker_id IS NULL "
            "AND lease_token_hash IS NULL AND lease_expires_at IS NULL "
            "AND last_heartbeat_at IS NULL"
            ")",
            name="ck_tasks_active_lease",
        ),
        CheckConstraint(
            "("
            "status = 'succeeded' AND result IS NOT NULL "
            "AND COALESCE(jsonb_typeof(result) = 'object', false)"
            ") OR (status <> 'succeeded' AND result IS NULL)",
            name="ck_tasks_result_state",
        ),
        CheckConstraint(
            "cancellation_requested_at IS NULL OR status IN ('running', 'cancelled')",
            name="ck_tasks_cancellation_state",
        ),
        CheckConstraint(
            "lease_token_hash IS NULL OR lease_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_tasks_lease_token_hash",
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
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(255))
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_summary: Mapped[str | None] = mapped_column(String(500))
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


class TaskAttempt(Base):
    """Append-only operational evidence for one claimed execution."""

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_task_attempts_task_number",
        ),
        CheckConstraint("attempt_number > 0", name="ck_task_attempts_number_positive"),
        CheckConstraint(
            "lease_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_task_attempts_lease_token_hash",
        ),
        CheckConstraint(
            "status IN "
            "('running', 'succeeded', 'retry_scheduled', 'failed', 'cancelled', "
            "'lease_expired')",
            name="ck_task_attempts_status",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) "
            "OR (status <> 'running' AND finished_at IS NOT NULL)",
            name="ck_task_attempts_finished_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", name="fk_task_attempts_task_id"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    lease_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(
            AttemptStatus,
            name="task_attempt_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda enum: [attempt_status.value for attempt_status in enum],
        ),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    terminal_reason: Mapped[str | None] = mapped_column(String(100))
