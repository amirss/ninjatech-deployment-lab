"""Create the persistent tasks table.

Revision ID: 0002_create_tasks
Revises: 0001_baseline
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_create_tasks"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create task persistence and database-backed lifecycle integrity."""
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("task_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_tasks_idempotency_key_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(task_input) = 'object'",
            name="ck_tasks_input_object",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_tasks_request_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN "
            "('pending_approval', 'approved', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "task_type ~ '^[a-z][a-z0-9_]{0,99}$'",
            name="ck_tasks_task_type",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
    )


def downgrade() -> None:
    """Remove task persistence without changing the baseline revision."""
    op.drop_table("tasks")
