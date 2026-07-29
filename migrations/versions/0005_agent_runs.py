"""Add fenced, resumable evidence for bounded code-proposal runs.

Revision ID: 0005_agent_runs
Revises: 0004_enterprise_integrations
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_agent_runs"
down_revision: str | None = "0004_enterprise_integrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create semantic agent runs, structured steps, and relational evidence links."""
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("originating_task_id", sa.Uuid(), nullable=False),
        sa.Column("bound_task_id", sa.Uuid(), nullable=False),
        sa.Column("bound_task_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("run_scope_key", sa.String(length=1000), nullable=False),
        sa.Column("workflow_version", sa.String(length=100), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_contract_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("completed_outcome", sa.String(length=50)),
        sa.Column("maximum_steps", sa.Integer(), nullable=False),
        sa.Column("completed_steps", sa.Integer(), nullable=False),
        sa.Column("model_call_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("proposal_fingerprint", sa.String(length=64)),
        sa.Column("proposal_size_bytes", sa.Integer()),
        sa.Column("safe_error_code", sa.String(length=100)),
        sa.Column("resume_not_before", sa.DateTime(timezone=True)),
        sa.Column("input_token_count", sa.Integer(), nullable=False),
        sa.Column("output_token_count", sa.Integer(), nullable=False),
        sa.Column("data_classification", sa.String(length=30), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
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
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('reserved', 'running', 'retryable', 'interrupted', 'completed', 'failed')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint(
            "completed_outcome IS NULL OR completed_outcome IN "
            "('proposed', 'needs_human_review', 'refused')",
            name="ck_agent_runs_outcome",
        ),
        sa.CheckConstraint(
            "source_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_runs_source_snapshot_hash",
        ),
        sa.CheckConstraint(
            "prompt_contract_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_runs_prompt_contract_hash",
        ),
        sa.CheckConstraint(
            "provider IN ('recorded', 'openai')",
            name="ck_agent_runs_provider",
        ),
        sa.CheckConstraint(
            "proposal_fingerprint IS NULL OR proposal_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_agent_runs_proposal_fingerprint",
        ),
        sa.CheckConstraint("maximum_steps >= 1", name="ck_agent_runs_maximum_steps"),
        sa.CheckConstraint(
            "completed_steps >= 0 AND completed_steps <= maximum_steps",
            name="ck_agent_runs_completed_steps",
        ),
        sa.CheckConstraint(
            "model_call_count >= 0 AND tool_call_count >= 0 "
            "AND input_token_count >= 0 AND output_token_count >= 0",
            name="ck_agent_runs_nonnegative_counters",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_outcome IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND completed_outcome IS NULL AND completed_at IS NULL)",
            name="ck_agent_runs_completion_state",
        ),
        sa.CheckConstraint(
            "(completed_outcome = 'proposed' AND jsonb_typeof(proposal) = 'object' "
            "AND proposal_fingerprint IS NOT NULL AND proposal_size_bytes > 0) OR "
            "(completed_outcome IS DISTINCT FROM 'proposed' AND proposal IS NULL "
            "AND proposal_fingerprint IS NULL AND proposal_size_bytes IS NULL)",
            name="ck_agent_runs_proposal_state",
        ),
        sa.CheckConstraint(
            "(status = 'retryable' AND safe_error_code IS NOT NULL "
            "AND resume_not_before IS NOT NULL) OR "
            "(status <> 'retryable' AND resume_not_before IS NULL)",
            name="ck_agent_runs_retry_state",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR safe_error_code IS NOT NULL",
            name="ck_agent_runs_failed_error",
        ),
        sa.CheckConstraint(
            "retention_until > created_at",
            name="ck_agent_runs_retention",
        ),
        sa.CheckConstraint(
            "data_classification IN ('public', 'internal', 'confidential', 'restricted')",
            name="ck_agent_runs_classification",
        ),
        sa.ForeignKeyConstraint(
            ["originating_task_id"],
            ["tasks.id"],
            name="fk_agent_runs_originating_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bound_task_id"],
            ["tasks.id"],
            name="fk_agent_runs_bound_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bound_task_attempt_id"],
            ["task_attempts.id"],
            name="fk_agent_runs_bound_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.UniqueConstraint("run_scope_key", name="uq_agent_runs_scope"),
    )
    op.create_index(
        "ix_agent_runs_task_created",
        "agent_runs",
        ["originating_task_id", "created_at"],
    )

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("logical_step_number", sa.Integer(), nullable=False),
        sa.Column("provider_call_number", sa.Integer()),
        sa.Column("step_kind", sa.String(length=60), nullable=False),
        sa.Column("tool_name", sa.String(length=100)),
        sa.Column("request_fingerprint", sa.String(length=64)),
        sa.Column("response_fingerprint", sa.String(length=64)),
        sa.Column("safe_action_kind", sa.String(length=60)),
        sa.Column("action_summary", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("tool_result_summary", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("input_token_count", sa.Integer(), nullable=False),
        sa.Column("output_token_count", sa.Integer(), nullable=False),
        sa.Column("duration_milliseconds", sa.Integer(), nullable=False),
        sa.Column("safe_error_code", sa.String(length=100)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("sequence_number >= 1", name="ck_agent_steps_sequence"),
        sa.CheckConstraint("logical_step_number >= 0", name="ck_agent_steps_logical_step"),
        sa.CheckConstraint(
            "provider_call_number IS NULL OR provider_call_number >= 1",
            name="ck_agent_steps_provider_call",
        ),
        sa.CheckConstraint(
            "step_kind IN ('run_reserved', 'run_rebound', 'completed_run_replayed', "
            "'model_call_started', 'model_response_recorded', 'path_search_completed', "
            "'file_read_completed', 'proposal_validation_completed', "
            "'source_drift_detected', 'run_retryable', 'run_interrupted', "
            "'run_completed', 'run_failed')",
            name="ck_agent_steps_kind",
        ),
        sa.CheckConstraint(
            "request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_agent_steps_request_fingerprint",
        ),
        sa.CheckConstraint(
            "response_fingerprint IS NULL OR response_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_agent_steps_response_fingerprint",
        ),
        sa.CheckConstraint(
            "input_token_count >= 0 AND output_token_count >= 0 AND duration_milliseconds >= 0",
            name="ck_agent_steps_nonnegative_values",
        ),
        sa.CheckConstraint(
            "action_summary IS NULL OR jsonb_typeof(action_summary) = 'object'",
            name="ck_agent_steps_action_summary",
        ),
        sa.CheckConstraint(
            "tool_result_summary IS NULL OR jsonb_typeof(tool_result_summary) = 'object'",
            name="ck_agent_steps_tool_summary",
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name="fk_agent_steps_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_agent_steps_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_attempt_id"],
            ["task_attempts.id"],
            name="fk_agent_steps_task_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_steps"),
        sa.UniqueConstraint(
            "agent_run_id",
            "sequence_number",
            name="uq_agent_steps_run_sequence",
        ),
    )
    op.create_index(
        "ix_agent_steps_run_created",
        "agent_steps",
        ["agent_run_id", "sequence_number"],
    )

    op.create_table(
        "agent_step_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_step_id", sa.Uuid(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("evidence_role", sa.String(length=40), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name="ck_agent_step_sources_ordinal"),
        sa.CheckConstraint(
            "evidence_role IN ('policy_input', 'requirement_input', "
            "'repository_manifest', 'repository_file', 'proposal_citation', 'drift_check')",
            name="ck_agent_step_sources_role",
        ),
        sa.ForeignKeyConstraint(
            ["agent_step_id"],
            ["agent_steps.id"],
            name="fk_agent_step_sources_step",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["source_artifacts.id"],
            name="fk_agent_step_sources_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_step_sources"),
        sa.UniqueConstraint(
            "agent_step_id",
            "source_artifact_id",
            "evidence_role",
            name="uq_agent_step_sources_identity",
        ),
        sa.UniqueConstraint(
            "agent_step_id",
            "ordinal",
            name="uq_agent_step_sources_ordinal",
        ),
    )
    op.create_index(
        "ix_agent_step_sources_source",
        "agent_step_sources",
        ["source_artifact_id", "agent_step_id"],
    )


def downgrade() -> None:
    """Remove only Milestone 5A1 run and evidence objects."""
    op.drop_index("ix_agent_step_sources_source", table_name="agent_step_sources")
    op.drop_table("agent_step_sources")
    op.drop_index("ix_agent_steps_run_created", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_runs_task_created", table_name="agent_runs")
    op.drop_table("agent_runs")
