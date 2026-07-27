"""Add minimized source provenance and the fenced external-action ledger.

Revision ID: 0004_enterprise_integrations
Revises: 0003_reliable_worker_execution
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_enterprise_integrations"
down_revision: str | None = "0003_reliable_worker_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create immutable source evidence and business-scoped action records."""
    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("observed_by_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("provider_resource_identifier", sa.String(length=500), nullable=False),
        sa.Column("canonical_source_url", sa.String(length=2000), nullable=False),
        sa.Column("source_version", sa.String(length=500), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("data_classification", sa.String(length=30), nullable=False),
        sa.Column("redaction_applied", sa.Boolean(), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "normalized_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('service_catalog', 'jira', 'github')",
            name="ck_source_artifacts_provider",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object'",
            name="ck_source_artifacts_payload_object",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_source_artifacts_content_hash",
        ),
        sa.CheckConstraint(
            "payload_size_bytes > 0",
            name="ck_source_artifacts_size_positive",
        ),
        sa.CheckConstraint(
            "retention_until > fetched_at",
            name="ck_source_artifacts_retention",
        ),
        sa.CheckConstraint(
            "data_classification IN ('public', 'internal', 'confidential', 'restricted')",
            name="ck_source_artifacts_classification",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_source_artifacts_task_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["observed_by_attempt_id"],
            ["task_attempts.id"],
            name="fk_source_artifacts_observed_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_artifacts"),
        sa.UniqueConstraint(
            "task_id",
            "provider",
            "resource_type",
            "provider_resource_identifier",
            "source_version",
            "content_hash",
            name="uq_source_artifacts_observation",
        ),
    )
    op.create_index(
        "ix_source_artifacts_task_created",
        "source_artifacts",
        ["task_id", "created_at"],
    )

    op.create_table(
        "external_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("current_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("action_scope_key", sa.String(length=1000), nullable=False),
        sa.Column("desired_request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("applied_request_fingerprint", sa.String(length=64)),
        sa.Column("decision_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("provider_resource_identifier", sa.String(length=500)),
        sa.Column("provider_url", sa.String(length=2000)),
        sa.Column("response_fingerprint", sa.String(length=64)),
        sa.Column("action_attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=100)),
        sa.Column("last_error_summary", sa.String(length=500)),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("write_started_at", sa.DateTime(timezone=True)),
        sa.Column("reconcile_not_before", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'reconciling', 'ready_to_execute', 'executing', "
            "'retryable_failure', 'outcome_unknown', 'succeeded', 'permanent_failure', "
            "'needs_human_review')",
            name="ck_external_actions_status",
        ),
        sa.CheckConstraint(
            "desired_request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_external_actions_desired_fingerprint",
        ),
        sa.CheckConstraint(
            "applied_request_fingerprint IS NULL OR applied_request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_external_actions_applied_fingerprint",
        ),
        sa.CheckConstraint(
            "decision_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_external_actions_snapshot_hash",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_external_actions_revision_positive"),
        sa.CheckConstraint(
            "action_attempt_count >= 1",
            name="ck_external_actions_attempt_count_positive",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND provider_resource_identifier IS NOT NULL "
            "AND applied_request_fingerprint IS NOT NULL AND completed_at IS NOT NULL) "
            "OR (status <> 'succeeded' AND completed_at IS NULL)",
            name="ck_external_actions_completion",
        ),
        sa.CheckConstraint(
            "status <> 'outcome_unknown' OR write_started_at IS NOT NULL",
            name="ck_external_actions_unknown_write",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_external_actions_task_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["current_attempt_id"],
            ["task_attempts.id"],
            name="fk_external_actions_current_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_actions"),
        sa.UniqueConstraint("action_scope_key", name="uq_external_actions_scope"),
    )
    op.create_index(
        "ix_external_actions_task_created",
        "external_actions",
        ["task_id", "created_at"],
    )

    op.create_table(
        "external_action_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_action_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("transition", sa.String(length=100), nullable=False),
        sa.Column("previous_status", sa.String(length=50)),
        sa.Column("new_status", sa.String(length=50), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence_number >= 1",
            name="ck_external_action_attempts_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["external_action_id"],
            ["external_actions.id"],
            name="fk_external_action_attempts_action",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_action_attempts_task_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_attempt_id"],
            ["task_attempts.id"],
            name="fk_action_attempts_task_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_action_attempts"),
        sa.UniqueConstraint(
            "external_action_id",
            "sequence_number",
            name="uq_external_action_attempts_sequence",
        ),
    )
    op.create_index(
        "ix_external_action_attempts_action_created",
        "external_action_attempts",
        ["external_action_id", "created_at"],
    )


def downgrade() -> None:
    """Remove integration evidence while preserving task execution history."""
    op.drop_index(
        "ix_external_action_attempts_action_created",
        table_name="external_action_attempts",
    )
    op.drop_table("external_action_attempts")
    op.drop_index("ix_external_actions_task_created", table_name="external_actions")
    op.drop_table("external_actions")
    op.drop_index("ix_source_artifacts_task_created", table_name="source_artifacts")
    op.drop_table("source_artifacts")
