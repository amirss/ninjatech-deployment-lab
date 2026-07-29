from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ninjatech_deployment_lab.database import Base
from ninjatech_deployment_lab.tasks.schemas import JsonValue


class AgentRunStatus(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    RETRYABLE = "retryable"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunOutcome(StrEnum):
    PROPOSED = "proposed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REFUSED = "refused"


class AgentStepKind(StrEnum):
    RUN_RESERVED = "run_reserved"
    RUN_REBOUND = "run_rebound"
    COMPLETED_RUN_REPLAYED = "completed_run_replayed"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_RESPONSE_RECORDED = "model_response_recorded"
    PATH_SEARCH_COMPLETED = "path_search_completed"
    FILE_READ_COMPLETED = "file_read_completed"
    PROPOSAL_VALIDATION_COMPLETED = "proposal_validation_completed"
    SOURCE_DRIFT_DETECTED = "source_drift_detected"
    RUN_RETRYABLE = "run_retryable"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class AgentRun(Base):
    """Semantic code-proposal run, replayable independently of task identity."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("run_scope_key", name="uq_agent_runs_scope"),
        CheckConstraint(
            "status IN ('reserved', 'running', 'retryable', 'interrupted', 'completed', 'failed')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            "completed_outcome IS NULL OR completed_outcome IN "
            "('proposed', 'needs_human_review', 'refused')",
            name="ck_agent_runs_outcome",
        ),
        CheckConstraint(
            "source_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_runs_source_snapshot_hash",
        ),
        CheckConstraint(
            "prompt_contract_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_runs_prompt_contract_hash",
        ),
        CheckConstraint(
            "provider IN ('recorded', 'openai')",
            name="ck_agent_runs_provider",
        ),
        CheckConstraint(
            "proposal_fingerprint IS NULL OR proposal_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_agent_runs_proposal_fingerprint",
        ),
        CheckConstraint("maximum_steps >= 1", name="ck_agent_runs_maximum_steps"),
        CheckConstraint(
            "completed_steps >= 0 AND completed_steps <= maximum_steps",
            name="ck_agent_runs_completed_steps",
        ),
        CheckConstraint(
            "model_call_count >= 0 AND tool_call_count >= 0 "
            "AND input_token_count >= 0 AND output_token_count >= 0",
            name="ck_agent_runs_nonnegative_counters",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_outcome IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND completed_outcome IS NULL AND completed_at IS NULL)",
            name="ck_agent_runs_completion_state",
        ),
        CheckConstraint(
            "(completed_outcome = 'proposed' AND jsonb_typeof(proposal) = 'object' "
            "AND proposal_fingerprint IS NOT NULL AND proposal_size_bytes > 0) OR "
            "(completed_outcome IS DISTINCT FROM 'proposed' AND proposal IS NULL "
            "AND proposal_fingerprint IS NULL AND proposal_size_bytes IS NULL)",
            name="ck_agent_runs_proposal_state",
        ),
        CheckConstraint(
            "(status = 'retryable' AND safe_error_code IS NOT NULL "
            "AND resume_not_before IS NOT NULL) OR "
            "(status <> 'retryable' AND resume_not_before IS NULL)",
            name="ck_agent_runs_retry_state",
        ),
        CheckConstraint(
            "status <> 'failed' OR safe_error_code IS NOT NULL",
            name="ck_agent_runs_failed_error",
        ),
        CheckConstraint(
            "retention_until > created_at",
            name="ck_agent_runs_retention",
        ),
        CheckConstraint(
            "data_classification IN ('public', 'internal', 'confidential', 'restricted')",
            name="ck_agent_runs_classification",
        ),
        Index("ix_agent_runs_task_created", "originating_task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    originating_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_agent_runs_originating_task"),
        nullable=False,
    )
    bound_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_agent_runs_bound_task"),
        nullable=False,
    )
    bound_task_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "task_attempts.id",
            ondelete="RESTRICT",
            name="fk_agent_runs_bound_attempt",
        ),
        nullable=False,
    )
    run_scope_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    completed_outcome: Mapped[str | None] = mapped_column(String(50))
    maximum_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposal: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    proposal_fingerprint: Mapped[str | None] = mapped_column(String(64))
    proposal_size_bytes: Mapped[int | None] = mapped_column(Integer)
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    resume_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_classification: Mapped[str] = mapped_column(String(30), nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentStep(Base):
    """Append-only structured evidence for resumable code-proposal work."""

    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "sequence_number", name="uq_agent_steps_run_sequence"),
        CheckConstraint("sequence_number >= 1", name="ck_agent_steps_sequence"),
        CheckConstraint("logical_step_number >= 0", name="ck_agent_steps_logical_step"),
        CheckConstraint(
            "provider_call_number IS NULL OR provider_call_number >= 1",
            name="ck_agent_steps_provider_call",
        ),
        CheckConstraint(
            "step_kind IN ('run_reserved', 'run_rebound', 'completed_run_replayed', "
            "'model_call_started', 'model_response_recorded', 'path_search_completed', "
            "'file_read_completed', 'proposal_validation_completed', "
            "'source_drift_detected', 'run_retryable', 'run_interrupted', "
            "'run_completed', 'run_failed')",
            name="ck_agent_steps_kind",
        ),
        CheckConstraint(
            "request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_agent_steps_request_fingerprint",
        ),
        CheckConstraint(
            "response_fingerprint IS NULL OR response_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_agent_steps_response_fingerprint",
        ),
        CheckConstraint(
            "input_token_count >= 0 AND output_token_count >= 0 AND duration_milliseconds >= 0",
            name="ck_agent_steps_nonnegative_values",
        ),
        CheckConstraint(
            "action_summary IS NULL OR jsonb_typeof(action_summary) = 'object'",
            name="ck_agent_steps_action_summary",
        ),
        CheckConstraint(
            "tool_result_summary IS NULL OR jsonb_typeof(tool_result_summary) = 'object'",
            name="ck_agent_steps_tool_summary",
        ),
        Index("ix_agent_steps_run_created", "agent_run_id", "sequence_number"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE", name="fk_agent_steps_run"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_agent_steps_task"),
        nullable=False,
    )
    task_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "task_attempts.id",
            ondelete="RESTRICT",
            name="fk_agent_steps_task_attempt",
        ),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_call_number: Mapped[int | None] = mapped_column(Integer)
    step_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(100))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    response_fingerprint: Mapped[str | None] = mapped_column(String(64))
    safe_action_kind: Mapped[str | None] = mapped_column(String(60))
    action_summary: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    tool_result_summary: Mapped[dict[str, JsonValue] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_milliseconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentStepSource(Base):
    """Relational evidence link between a structured step and source artifact."""

    __tablename__ = "agent_step_sources"
    __table_args__ = (
        UniqueConstraint(
            "agent_step_id",
            "source_artifact_id",
            "evidence_role",
            name="uq_agent_step_sources_identity",
        ),
        UniqueConstraint(
            "agent_step_id",
            "ordinal",
            name="uq_agent_step_sources_ordinal",
        ),
        CheckConstraint("ordinal >= 1", name="ck_agent_step_sources_ordinal"),
        CheckConstraint(
            "evidence_role IN ('policy_input', 'requirement_input', "
            "'repository_manifest', 'repository_file', 'proposal_citation', 'drift_check')",
            name="ck_agent_step_sources_role",
        ),
        Index("ix_agent_step_sources_source", "source_artifact_id", "agent_step_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_step_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "agent_steps.id",
            ondelete="CASCADE",
            name="fk_agent_step_sources_step",
        ),
        nullable=False,
    )
    source_artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "source_artifacts.id",
            ondelete="RESTRICT",
            name="fk_agent_step_sources_source",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_role: Mapped[str] = mapped_column(String(40), nullable=False)
