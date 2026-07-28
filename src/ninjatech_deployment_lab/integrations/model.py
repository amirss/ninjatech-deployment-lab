from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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


class ExternalActionStatus(StrEnum):
    RESERVED = "reserved"
    RECONCILING = "reconciling"
    READY_TO_EXECUTE = "ready_to_execute"
    EXECUTING = "executing"
    RETRYABLE_FAILURE = "retryable_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SUCCEEDED = "succeeded"
    PERMANENT_FAILURE = "permanent_failure"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class SourceArtifact(Base):
    """Immutable minimized evidence observed by one task attempt."""

    __tablename__ = "source_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "provider",
            "resource_type",
            "provider_resource_identifier",
            "source_version",
            "content_hash",
            name="uq_source_artifacts_observation",
        ),
        CheckConstraint(
            "provider IN ('service_catalog', 'jira', 'github')",
            name="ck_source_artifacts_provider",
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object'",
            name="ck_source_artifacts_payload_object",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_source_artifacts_content_hash",
        ),
        CheckConstraint("payload_size_bytes > 0", name="ck_source_artifacts_size_positive"),
        CheckConstraint(
            "retention_until > fetched_at",
            name="ck_source_artifacts_retention",
        ),
        CheckConstraint(
            "data_classification IN ('public', 'internal', 'confidential', 'restricted')",
            name="ck_source_artifacts_classification",
        ),
        Index("ix_source_artifacts_task_created", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_source_artifacts_task_id"),
        nullable=False,
    )
    observed_by_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "task_attempts.id",
            ondelete="RESTRICT",
            name="fk_source_artifacts_observed_attempt",
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_resource_identifier: Mapped[str] = mapped_column(String(500), nullable=False)
    canonical_source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    source_version: Mapped[str] = mapped_column(String(500), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data_classification: Mapped[str] = mapped_column(String(30), nullable=False)
    redaction_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    normalized_payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ExternalAction(Base):
    """Current state of one business-scoped provider side effect."""

    __tablename__ = "external_actions"
    __table_args__ = (
        UniqueConstraint("action_scope_key", name="uq_external_actions_scope"),
        CheckConstraint(
            "status IN ('reserved', 'reconciling', 'ready_to_execute', 'executing', "
            "'retryable_failure', 'outcome_unknown', 'succeeded', 'permanent_failure', "
            "'needs_human_review')",
            name="ck_external_actions_status",
        ),
        CheckConstraint(
            "desired_request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_external_actions_desired_fingerprint",
        ),
        CheckConstraint(
            "applied_request_fingerprint IS NULL OR applied_request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_external_actions_applied_fingerprint",
        ),
        CheckConstraint(
            "decision_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_external_actions_snapshot_hash",
        ),
        CheckConstraint("revision >= 1", name="ck_external_actions_revision_positive"),
        CheckConstraint(
            "action_attempt_count >= 1",
            name="ck_external_actions_attempt_count_positive",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND provider_resource_identifier IS NOT NULL "
            "AND applied_request_fingerprint IS NOT NULL AND completed_at IS NOT NULL) "
            "OR (status <> 'succeeded' AND completed_at IS NULL)",
            name="ck_external_actions_completion",
        ),
        CheckConstraint(
            "status <> 'outcome_unknown' OR write_started_at IS NOT NULL",
            name="ck_external_actions_unknown_write",
        ),
        Index("ix_external_actions_task_created", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_external_actions_task_id"),
        nullable=False,
    )
    current_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "task_attempts.id",
            ondelete="RESTRICT",
            name="fk_external_actions_current_attempt",
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    action_scope_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    desired_request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    decision_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_resource_identifier: Mapped[str | None] = mapped_column(String(500))
    provider_url: Mapped[str | None] = mapped_column(String(2000))
    response_fingerprint: Mapped[str | None] = mapped_column(String(64))
    action_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_summary: Mapped[str | None] = mapped_column(String(500))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    write_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ExternalActionAttempt(Base):
    """Append-only transition evidence for an external business action."""

    __tablename__ = "external_action_attempts"
    __table_args__ = (
        UniqueConstraint(
            "external_action_id",
            "sequence_number",
            name="uq_external_action_attempts_sequence",
        ),
        CheckConstraint(
            "sequence_number >= 1",
            name="ck_external_action_attempts_sequence_positive",
        ),
        Index(
            "ix_external_action_attempts_action_created",
            "external_action_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    external_action_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "external_actions.id",
            ondelete="CASCADE",
            name="fk_external_action_attempts_action",
        ),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_action_attempts_task_id"),
        nullable=False,
    )
    task_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "task_attempts.id",
            ondelete="RESTRICT",
            name="fk_action_attempts_task_attempt",
        ),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    transition: Mapped[str] = mapped_column(String(100), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(50))
    new_status: Mapped[str] = mapped_column(String(50), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
