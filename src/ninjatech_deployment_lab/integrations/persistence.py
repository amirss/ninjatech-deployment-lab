from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    SourceReference,
    canonical_json,
    canonical_source_url,
    sha256_json,
)
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionAttempt,
    ExternalActionStatus,
    SourceArtifact,
)
from ninjatech_deployment_lab.tasks.domain import AttemptStatus, TaskStatus
from ninjatech_deployment_lab.tasks.model import Task, TaskAttempt
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import ExecutionFence, ExecutionInvariantError

logger = logging.getLogger(__name__)


class ActionReservationKind(StrEnum):
    CREATED = "created"
    REPLAY = "replay"
    CHANGED = "changed"
    SOURCE_DRIFT = "source_drift"


@dataclass(frozen=True, slots=True)
class ActionReservation:
    action: ExternalAction
    kind: ActionReservationKind


class SourceArtifactRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        maximum_bytes: int,
    ) -> None:
        self._session_factory = session_factory
        self._maximum_bytes = maximum_bytes

    async def record(
        self,
        *,
        fence: ExecutionFence,
        provider: str,
        resource_type: str,
        provider_resource_identifier: str,
        source_url: str,
        source_version: str,
        data_classification: DataClassification,
        redaction_applied: bool,
        normalized_payload: dict[str, JsonValue],
        fetched_at: datetime,
    ) -> SourceArtifact:
        serialized = canonical_json(normalized_payload)
        if not serialized or len(serialized) > self._maximum_bytes:
            raise ValueError("source artifact exceeds its configured size limit")
        content_hash = sha256_json(normalized_payload)
        retention_days = {
            DataClassification.PUBLIC: 90,
            DataClassification.INTERNAL: 30,
            DataClassification.CONFIDENTIAL: 7,
            DataClassification.RESTRICTED: 1,
        }[data_classification]
        async with self._session_factory() as session:
            async with session.begin():
                database_now = await _database_now(session)
                await _assert_active_fence(session, fence, database_now)
                statement = (
                    pg_insert(SourceArtifact)
                    .values(
                        id=uuid4(),
                        task_id=fence.task_id,
                        observed_by_attempt_id=fence.attempt_id,
                        provider=provider,
                        resource_type=resource_type,
                        provider_resource_identifier=provider_resource_identifier,
                        canonical_source_url=canonical_source_url(source_url),
                        source_version=source_version,
                        schema_version=1,
                        data_classification=data_classification.value,
                        redaction_applied=redaction_applied,
                        retention_until=max(database_now, fetched_at)
                        + timedelta(days=retention_days),
                        normalized_payload=normalized_payload,
                        content_hash=content_hash,
                        payload_size_bytes=len(serialized),
                        fetched_at=fetched_at,
                    )
                    .on_conflict_do_nothing(constraint="uq_source_artifacts_observation")
                    .returning(SourceArtifact.id)
                )
                artifact_id = (await session.execute(statement)).scalar_one_or_none()
                if artifact_id is None:
                    artifact_id = (
                        await session.execute(
                            select(SourceArtifact.id).where(
                                SourceArtifact.task_id == fence.task_id,
                                SourceArtifact.provider == provider,
                                SourceArtifact.resource_type == resource_type,
                                SourceArtifact.provider_resource_identifier
                                == provider_resource_identifier,
                                SourceArtifact.source_version == source_version,
                                SourceArtifact.content_hash == content_hash,
                            )
                        )
                    ).scalar_one()
                artifact = (
                    await session.execute(
                        select(SourceArtifact).where(SourceArtifact.id == artifact_id)
                    )
                ).scalar_one()
            logger.info(
                "source_artifact_recorded",
                extra={
                    "event": "source_artifact_recorded",
                    "artifact_id": str(artifact.id),
                    "task_id": str(fence.task_id),
                    "attempt_id": str(fence.attempt_id),
                    "provider": artifact.provider,
                    "resource_type": artifact.resource_type,
                },
            )
            return artifact


class ExternalActionRepository:
    """Fenced state transitions with one atomic append-only evidence row."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reserve_or_get(
        self,
        *,
        fence: ExecutionFence,
        action_scope_key: str,
        desired_request_fingerprint: str,
        decision_snapshot_hash: str,
    ) -> ActionReservation:
        async with self._session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                await _assert_active_fence(session, fence, now)
                action_id = uuid4()
                inserted_id = (
                    await session.execute(
                        pg_insert(ExternalAction)
                        .values(
                            id=action_id,
                            task_id=fence.task_id,
                            current_attempt_id=fence.attempt_id,
                            provider="github",
                            operation="upsert_deployment_context_comment",
                            action_scope_key=action_scope_key,
                            desired_request_fingerprint=desired_request_fingerprint,
                            decision_snapshot_hash=decision_snapshot_hash,
                            revision=1,
                            status=ExternalActionStatus.RESERVED.value,
                            action_attempt_count=1,
                            reserved_at=now,
                            last_attempt_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(constraint="uq_external_actions_scope")
                        .returning(ExternalAction.id)
                    )
                ).scalar_one_or_none()
                if inserted_id is not None:
                    action = await self._lock_action(session, inserted_id)
                    session.add(
                        _attempt(
                            action,
                            fence,
                            sequence_number=1,
                            transition="reserved",
                            previous_status=None,
                            new_status=ExternalActionStatus.RESERVED,
                        )
                    )
                    await session.flush()
                    logger.info(
                        "external_action_reserved",
                        extra={
                            "event": "external_action_reserved",
                            "action_id": str(action.id),
                            "task_id": str(fence.task_id),
                            "attempt_id": str(fence.attempt_id),
                            "provider": action.provider,
                            "revision": action.revision,
                            "action_status": action.status,
                        },
                    )
                    return ActionReservation(action, ActionReservationKind.CREATED)

                action = (
                    await session.execute(
                        select(ExternalAction)
                        .where(ExternalAction.action_scope_key == action_scope_key)
                        .with_for_update()
                    )
                ).scalar_one()
                if action.desired_request_fingerprint == desired_request_fingerprint:
                    kind = (
                        ActionReservationKind.REPLAY
                        if action.decision_snapshot_hash == decision_snapshot_hash
                        else ActionReservationKind.SOURCE_DRIFT
                    )
                else:
                    kind = ActionReservationKind.CHANGED
                await self._append_same_status(
                    session,
                    action=action,
                    fence=fence,
                    now=now,
                    transition=f"reservation_{kind.value}",
                )
                logger.info(
                    "external_action_replayed",
                    extra={
                        "event": "external_action_replayed",
                        "action_id": str(action.id),
                        "task_id": str(fence.task_id),
                        "attempt_id": str(fence.attempt_id),
                        "provider": action.provider,
                        "revision": action.revision,
                        "action_status": action.status,
                    },
                )
                return ActionReservation(action, kind)

    async def transition(
        self,
        *,
        fence: ExecutionFence,
        action_id: UUID,
        expected_statuses: set[ExternalActionStatus],
        new_status: ExternalActionStatus,
        transition: str,
        values: dict[str, Any] | None = None,
        error_code: str | None = None,
        settlement_delay_seconds: float | None = None,
    ) -> ExternalAction:
        async with self._session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                await _assert_active_fence(session, fence, now)
                action = await self._lock_action(session, action_id)
                previous = ExternalActionStatus(action.status)
                if previous not in expected_statuses:
                    raise ExecutionInvariantError("external action state changed unexpectedly")
                sequence = action.action_attempt_count + 1
                update_values: dict[str, Any] = {
                    "status": new_status.value,
                    "task_id": fence.task_id,
                    "current_attempt_id": fence.attempt_id,
                    "action_attempt_count": sequence,
                    "last_attempt_at": now,
                    "updated_at": now,
                    "last_error_code": error_code,
                    "last_error_summary": (
                        "External provider action requires attention"
                        if error_code is not None
                        else None
                    ),
                }
                update_values.update(values or {})
                if new_status is ExternalActionStatus.SUCCEEDED:
                    update_values["completed_at"] = now
                if settlement_delay_seconds is not None:
                    update_values["write_started_at"] = now
                    update_values["reconcile_not_before"] = now + timedelta(
                        seconds=settlement_delay_seconds
                    )
                result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(ExternalAction)
                        .where(
                            ExternalAction.id == action.id,
                            ExternalAction.status == previous.value,
                            ExternalAction.action_attempt_count == action.action_attempt_count,
                        )
                        .values(**update_values)
                    ),
                )
                _require_one_row(result, "external action transition")
                session.add(
                    _attempt(
                        action,
                        fence,
                        sequence_number=sequence,
                        transition=transition,
                        previous_status=previous,
                        new_status=new_status,
                        error_code=error_code,
                    )
                )
                await session.flush()
                updated = await self._lock_action(session, action.id)
                logger.info(
                    "external_action_transition",
                    extra={
                        "event": f"external_action_{transition}",
                        "action_id": str(updated.id),
                        "task_id": str(fence.task_id),
                        "attempt_id": str(fence.attempt_id),
                        "provider": updated.provider,
                        "previous_status": previous.value,
                        "new_status": new_status.value,
                        "revision": updated.revision,
                        "error_code": error_code,
                    },
                )
                return updated

    async def revise(
        self,
        *,
        fence: ExecutionFence,
        action_id: UUID,
        expected_revision: int,
        desired_request_fingerprint: str,
        decision_snapshot_hash: str,
    ) -> ExternalAction:
        async with self._session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                await _assert_active_fence(session, fence, now)
                action = await self._lock_action(session, action_id)
                if (
                    ExternalActionStatus(action.status) is not ExternalActionStatus.SUCCEEDED
                    or action.revision != expected_revision
                    or action.provider_resource_identifier is None
                ):
                    raise ExecutionInvariantError("external action cannot be revised")
                sequence = action.action_attempt_count + 1
                result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(ExternalAction)
                        .where(
                            ExternalAction.id == action.id,
                            ExternalAction.revision == expected_revision,
                            ExternalAction.status == ExternalActionStatus.SUCCEEDED.value,
                        )
                        .values(
                            task_id=fence.task_id,
                            current_attempt_id=fence.attempt_id,
                            desired_request_fingerprint=desired_request_fingerprint,
                            decision_snapshot_hash=decision_snapshot_hash,
                            revision=expected_revision + 1,
                            status=ExternalActionStatus.RESERVED.value,
                            completed_at=None,
                            action_attempt_count=sequence,
                            last_attempt_at=now,
                            updated_at=now,
                        )
                    ),
                )
                _require_one_row(result, "external action revision")
                action.revision = expected_revision + 1
                session.add(
                    _attempt(
                        action,
                        fence,
                        sequence_number=sequence,
                        transition="revision_reserved",
                        previous_status=ExternalActionStatus.SUCCEEDED,
                        new_status=ExternalActionStatus.RESERVED,
                    )
                )
                await session.flush()
                return await self._lock_action(session, action.id)

    async def get(self, action_id: UUID) -> ExternalAction:
        async with self._session_factory() as session:
            return (
                await session.execute(select(ExternalAction).where(ExternalAction.id == action_id))
            ).scalar_one()

    async def reconciliation_delay_seconds(self, action_id: UUID) -> float:
        """Use PostgreSQL time to avoid application-clock skew in write holdoff."""
        async with self._session_factory() as session:
            delay = (
                await session.execute(
                    select(
                        func.greatest(
                            0.0,
                            func.extract(
                                "epoch",
                                ExternalAction.reconcile_not_before - func.clock_timestamp(),
                            ),
                        )
                    ).where(
                        ExternalAction.id == action_id,
                        ExternalAction.reconcile_not_before.is_not(None),
                    )
                )
            ).scalar_one_or_none()
            return 0.0 if delay is None else float(delay)

    async def _append_same_status(
        self,
        session: AsyncSession,
        *,
        action: ExternalAction,
        fence: ExecutionFence,
        now: datetime,
        transition: str,
    ) -> None:
        sequence = action.action_attempt_count + 1
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(ExternalAction)
                .where(
                    ExternalAction.id == action.id,
                    ExternalAction.action_attempt_count == action.action_attempt_count,
                )
                .values(
                    task_id=fence.task_id,
                    current_attempt_id=fence.attempt_id,
                    action_attempt_count=sequence,
                    last_attempt_at=now,
                    updated_at=now,
                )
            ),
        )
        _require_one_row(result, "external action replay")
        session.add(
            _attempt(
                action,
                fence,
                sequence_number=sequence,
                transition=transition,
                previous_status=ExternalActionStatus(action.status),
                new_status=ExternalActionStatus(action.status),
            )
        )
        await session.flush()

    @staticmethod
    async def _lock_action(session: AsyncSession, action_id: UUID) -> ExternalAction:
        return (
            await session.execute(
                select(ExternalAction)
                .where(ExternalAction.id == action_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()


def source_reference(artifact: SourceArtifact) -> SourceReference:
    return SourceReference(
        artifact_id=artifact.id,
        provider=artifact.provider,
        resource_type=artifact.resource_type,
        provider_resource_identifier=artifact.provider_resource_identifier,
        source_version=artifact.source_version,
        content_hash=artifact.content_hash,
    )


async def _assert_active_fence(
    session: AsyncSession,
    fence: ExecutionFence,
    database_now: datetime,
) -> None:
    active = (
        await session.execute(
            select(Task.id)
            .join(
                TaskAttempt,
                and_(
                    TaskAttempt.id == fence.attempt_id,
                    TaskAttempt.task_id == Task.id,
                ),
            )
            .where(
                Task.id == fence.task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == fence.worker_id,
                Task.attempt_count == fence.attempt_number,
                Task.lease_token_hash == fence.lease_token_hash,
                Task.lease_expires_at > database_now,
                TaskAttempt.attempt_number == fence.attempt_number,
                TaskAttempt.lease_token_hash == fence.lease_token_hash,
                TaskAttempt.status == AttemptStatus.RUNNING,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if active is None:
        raise ExecutionInvariantError("active task execution fence was not confirmed")


def _attempt(
    action: ExternalAction,
    fence: ExecutionFence,
    *,
    sequence_number: int,
    transition: str,
    previous_status: ExternalActionStatus | None,
    new_status: ExternalActionStatus,
    error_code: str | None = None,
) -> ExternalActionAttempt:
    return ExternalActionAttempt(
        id=uuid4(),
        external_action_id=action.id,
        task_id=fence.task_id,
        task_attempt_id=fence.attempt_id,
        sequence_number=sequence_number,
        transition=transition,
        previous_status=previous_status.value if previous_status is not None else None,
        new_status=new_status.value,
        revision=action.revision,
        error_code=error_code,
    )


async def _database_now(session: AsyncSession) -> datetime:
    return cast(
        datetime,
        (await session.execute(select(func.clock_timestamp()))).scalar_one(),
    )


def _require_one_row(result: CursorResult[Any], operation: str) -> None:
    if result.rowcount != 1:
        raise ExecutionInvariantError(
            f"{operation} expected one row but affected {result.rowcount}"
        )
