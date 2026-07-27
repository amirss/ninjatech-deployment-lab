"""Add reliable worker execution state and durable attempt history.

Revision ID: 0003_reliable_worker_execution
Revises: 0002_create_tasks
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_reliable_worker_execution"
down_revision: str | None = "0002_create_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add queue coordination, fencing, results, and attempt evidence."""
    op.add_column(
        "tasks",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column("tasks", sa.Column("available_at", sa.DateTime(timezone=True)))
    op.add_column("tasks", sa.Column("worker_id", sa.String(length=255)))
    op.add_column("tasks", sa.Column("lease_token_hash", sa.String(length=64)))
    op.add_column("tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("tasks", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column(
        "tasks",
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
    )
    op.add_column("tasks", sa.Column("result", postgresql.JSONB(astext_type=sa.Text())))
    op.add_column("tasks", sa.Column("last_error_code", sa.String(length=100)))
    op.add_column("tasks", sa.Column("last_error_summary", sa.String(length=500)))

    # Milestone 2 had no execution process, so a persisted running row cannot
    # have a legitimate owner. Return it to the approved queue during upgrade.
    op.execute(
        """
        UPDATE tasks
        SET status = 'approved',
            available_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE status = 'running'
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET available_at = clock_timestamp()
        WHERE status = 'approved' AND available_at IS NULL
        """
    )

    op.create_check_constraint(
        "ck_tasks_attempt_count_nonnegative",
        "tasks",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_tasks_max_attempts_positive",
        "tasks",
        "max_attempts >= 1",
    )
    op.create_check_constraint(
        "ck_tasks_attempt_count_within_max",
        "tasks",
        "attempt_count <= max_attempts",
    )
    op.create_check_constraint(
        "ck_tasks_approved_available",
        "tasks",
        "status <> 'approved' OR available_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_tasks_active_lease",
        "tasks",
        "("
        "status = 'running' AND worker_id IS NOT NULL "
        "AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL "
        "AND last_heartbeat_at IS NOT NULL AND attempt_count > 0"
        ") OR ("
        "status <> 'running' AND worker_id IS NULL "
        "AND lease_token_hash IS NULL AND lease_expires_at IS NULL "
        "AND last_heartbeat_at IS NULL"
        ")",
    )
    op.create_check_constraint(
        "ck_tasks_result_state",
        "tasks",
        "("
        "status = 'succeeded' AND result IS NOT NULL "
        "AND COALESCE(jsonb_typeof(result) = 'object', false)"
        ") OR (status <> 'succeeded' AND result IS NULL)",
    )
    op.create_check_constraint(
        "ck_tasks_cancellation_state",
        "tasks",
        "cancellation_requested_at IS NULL OR status IN ('running', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_tasks_lease_token_hash",
        "tasks",
        "lease_token_hash IS NULL OR lease_token_hash ~ '^[0-9a-f]{64}$'",
    )

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("error_summary", sa.String(length=500)),
        sa.Column("terminal_reason", sa.String(length=100)),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_task_attempts_number_positive",
        ),
        sa.CheckConstraint(
            "lease_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_task_attempts_lease_token_hash",
        ),
        sa.CheckConstraint(
            "status IN "
            "('running', 'succeeded', 'retry_scheduled', 'failed', 'cancelled', "
            "'lease_expired')",
            name="ck_task_attempts_status",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) "
            "OR (status <> 'running' AND finished_at IS NOT NULL)",
            name="ck_task_attempts_finished_state",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_attempts_task_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_attempts"),
        sa.UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_task_attempts_task_number",
        ),
    )
    op.create_index(
        "ix_tasks_claimable",
        "tasks",
        ["available_at", "created_at", "id"],
        postgresql_where=sa.text("status = 'approved'"),
    )
    op.create_index(
        "ix_tasks_expired_leases",
        "tasks",
        ["lease_expires_at", "id"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Remove worker execution state and attempt history."""
    op.drop_index("ix_tasks_expired_leases", table_name="tasks")
    op.drop_index("ix_tasks_claimable", table_name="tasks")
    op.drop_table("task_attempts")

    for constraint_name in (
        "ck_tasks_lease_token_hash",
        "ck_tasks_cancellation_state",
        "ck_tasks_result_state",
        "ck_tasks_active_lease",
        "ck_tasks_approved_available",
        "ck_tasks_attempt_count_within_max",
        "ck_tasks_max_attempts_positive",
        "ck_tasks_attempt_count_nonnegative",
    ):
        op.drop_constraint(constraint_name, "tasks", type_="check")

    for column_name in (
        "last_error_summary",
        "last_error_code",
        "result",
        "cancellation_requested_at",
        "last_heartbeat_at",
        "lease_expires_at",
        "lease_token_hash",
        "worker_id",
        "available_at",
        "max_attempts",
        "attempt_count",
    ):
        op.drop_column("tasks", column_name)
