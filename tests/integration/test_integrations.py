from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import (
    create_database_engine,
    create_session_factory,
)
from ninjatech_deployment_lab.integrations.domain import DataClassification
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionAttempt,
    ExternalActionStatus,
    SourceArtifact,
)
from ninjatech_deployment_lab.integrations.persistence import (
    ActionReservation,
    ActionReservationKind,
    ExternalActionRepository,
    SourceArtifactRepository,
)
from ninjatech_deployment_lab.tasks.service import TaskService
from ninjatech_deployment_lab.worker.domain import (
    ClaimedTask,
    ExecutionFence,
    ExecutionInvariantError,
)
from ninjatech_deployment_lab.worker.repository import WorkerRepository

pytestmark = pytest.mark.postgres


async def _claim(
    database_url: str,
    *,
    idempotency_key: str,
    worker_id: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], ClaimedTask]:
    settings = Settings(database_url=database_url, environment="test")
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        created = await TaskService(session).create_task(
            idempotency_key=idempotency_key,
            task_type="deployment_context_sync",
            task_input={
                "jira_issue_key": "ENG-123",
                "github_repository": "customer/example-service",
                "github_issue_number": 42,
                "service_id": "payments-api",
            },
        )
        await TaskService(session).approve_task(created.task.id)
    claim = await WorkerRepository(session_factory).claim_one(
        supported_task_types=("deployment_context_sync",),
        worker_id=worker_id,
        lease_duration_seconds=30,
    )
    assert claim is not None
    return engine, session_factory, claim


def test_source_artifact_versions_are_immutable_and_deduplicated(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, claim = await _claim(
            postgres_database_url,
            idempotency_key=f"artifact-{uuid4()}",
            worker_id="artifact-worker",
        )
        try:
            repository = SourceArtifactRepository(session_factory, maximum_bytes=4096)
            first = await repository.record(
                fence=claim.execution_fence,
                provider="service_catalog",
                resource_type="service",
                provider_resource_identifier="payments-api",
                source_url="https://catalog.example/services/payments-api?secret=no",
                source_version="v7",
                data_classification=DataClassification.INTERNAL,
                redaction_applied=False,
                fetched_at=datetime.now(UTC),
                normalized_payload={"service_id": "payments-api", "policy_version": 7},
            )
            replay = await repository.record(
                fence=claim.execution_fence,
                provider="service_catalog",
                resource_type="service",
                provider_resource_identifier="payments-api",
                source_url="https://catalog.example/services/payments-api?secret=no",
                source_version="v7",
                data_classification=DataClassification.INTERNAL,
                redaction_applied=False,
                fetched_at=datetime.now(UTC),
                normalized_payload={"policy_version": 7, "service_id": "payments-api"},
            )
            changed = await repository.record(
                fence=claim.execution_fence,
                provider="service_catalog",
                resource_type="service",
                provider_resource_identifier="payments-api",
                source_url="https://catalog.example/services/payments-api?secret=no",
                source_version="v7",
                data_classification=DataClassification.INTERNAL,
                redaction_applied=False,
                fetched_at=datetime.now(UTC),
                normalized_payload={"service_id": "payments-api", "policy_version": 8},
            )
            assert first.id == replay.id
            assert changed.id != first.id
            assert first.canonical_source_url == ("https://catalog.example/services/payments-api")
            async with session_factory() as session:
                count = (
                    await session.execute(select(func.count()).select_from(SourceArtifact))
                ).scalar_one()
            assert count == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_action_scope_is_reserved_once_under_concurrency(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, claim = await _claim(
            postgres_database_url,
            idempotency_key=f"action-{uuid4()}",
            worker_id="action-worker",
        )
        try:
            repository = ExternalActionRepository(session_factory)

            async def reserve() -> ActionReservation:
                return await repository.reserve_or_get(
                    fence=claim.execution_fence,
                    action_scope_key=(
                        "deployment_context_sync:v1:test:424242:42:payments-api:github_comment"
                    ),
                    desired_request_fingerprint="a" * 64,
                    decision_snapshot_hash="b" * 64,
                )

            first, second = await asyncio.gather(
                reserve(),
                reserve(),
            )
            assert {first.kind, second.kind} == {
                ActionReservationKind.CREATED,
                ActionReservationKind.REPLAY,
            }
            async with session_factory() as session:
                action_count = (
                    await session.execute(select(func.count()).select_from(ExternalAction))
                ).scalar_one()
                attempt_count = (
                    await session.execute(select(func.count()).select_from(ExternalActionAttempt))
                ).scalar_one()
            assert action_count == 1
            assert attempt_count == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_independent_task_reuses_business_action_and_drift_is_explicit(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        first_engine, first_factory, first_claim = await _claim(
            postgres_database_url,
            idempotency_key=f"first-{uuid4()}",
            worker_id="first-worker",
        )
        second_engine, second_factory, second_claim = await _claim(
            postgres_database_url,
            idempotency_key=f"second-{uuid4()}",
            worker_id="second-worker",
        )
        scope = "deployment_context_sync:v1:test:424242:42:payments-api:github_comment"
        try:
            first_repository = ExternalActionRepository(first_factory)
            second_repository = ExternalActionRepository(second_factory)
            created = await first_repository.reserve_or_get(
                fence=first_claim.execution_fence,
                action_scope_key=scope,
                desired_request_fingerprint="a" * 64,
                decision_snapshot_hash="b" * 64,
            )
            replay = await second_repository.reserve_or_get(
                fence=second_claim.execution_fence,
                action_scope_key=scope,
                desired_request_fingerprint="a" * 64,
                decision_snapshot_hash="b" * 64,
            )
            drift = await second_repository.reserve_or_get(
                fence=second_claim.execution_fence,
                action_scope_key=scope,
                desired_request_fingerprint="a" * 64,
                decision_snapshot_hash="c" * 64,
            )
            changed = await second_repository.reserve_or_get(
                fence=second_claim.execution_fence,
                action_scope_key=scope,
                desired_request_fingerprint="d" * 64,
                decision_snapshot_hash="e" * 64,
            )
            assert created.action.id == replay.action.id
            assert replay.kind is ActionReservationKind.REPLAY
            assert drift.kind is ActionReservationKind.SOURCE_DRIFT
            assert changed.kind is ActionReservationKind.CHANGED
            async with second_factory() as session:
                count = (
                    await session.execute(select(func.count()).select_from(ExternalAction))
                ).scalar_one()
            assert count == 1
        finally:
            await first_engine.dispose()
            await second_engine.dispose()

    asyncio.run(scenario())


def test_stale_fence_cannot_update_action_or_attempt_history(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, claim = await _claim(
            postgres_database_url,
            idempotency_key=f"stale-{uuid4()}",
            worker_id="active-worker",
        )
        try:
            repository = ExternalActionRepository(session_factory)
            reservation = await repository.reserve_or_get(
                fence=claim.execution_fence,
                action_scope_key=(
                    "deployment_context_sync:v1:test:424242:42:payments-api:github_comment"
                ),
                desired_request_fingerprint="a" * 64,
                decision_snapshot_hash="b" * 64,
            )
            stale = ExecutionFence(
                task_id=claim.task_id,
                attempt_id=claim.attempt_id,
                attempt_number=claim.attempt_number,
                worker_id=claim.worker_id,
                lease_token_hash="f" * 64,
            )
            with pytest.raises(ExecutionInvariantError):
                await repository.transition(
                    fence=stale,
                    action_id=reservation.action.id,
                    expected_statuses={ExternalActionStatus.RESERVED},
                    new_status=ExternalActionStatus.RECONCILING,
                    transition="stale_reconciliation",
                )
            async with session_factory() as session:
                action = (
                    await session.execute(
                        select(ExternalAction).where(ExternalAction.id == reservation.action.id)
                    )
                ).scalar_one()
                attempts = (
                    await session.execute(select(func.count()).select_from(ExternalActionAttempt))
                ).scalar_one()
            assert action.status == ExternalActionStatus.RESERVED.value
            assert attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
