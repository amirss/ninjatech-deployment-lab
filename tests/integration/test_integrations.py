from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import (
    create_database_engine,
    create_session_factory,
)
from ninjatech_deployment_lab.integrations.connectors import (
    CommentSearchResult,
    GitHubComment,
    safe_marker,
)
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    DecisionOutcome,
    DecisionReasonCode,
    DeploymentContextDecision,
    sha256_json,
)
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
from ninjatech_deployment_lab.integrations.workflow import DeploymentContextSyncHandler
from ninjatech_deployment_lab.tasks.domain import TaskStatus
from ninjatech_deployment_lab.tasks.model import Task
from ninjatech_deployment_lab.tasks.service import TaskService
from ninjatech_deployment_lab.worker.domain import (
    ClaimedTask,
    ExecutionFence,
    ExecutionInvariantError,
    FenceResult,
    OwnershipLostError,
    TaskCancelled,
)
from ninjatech_deployment_lab.worker.handlers import HandlerContext, TaskExecution
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


class _AcceptedCommentGitHub:
    def __init__(
        self,
        *,
        customer_cancellation: asyncio.Event | None = None,
        ownership_lost: asyncio.Event | None = None,
    ) -> None:
        self.customer_cancellation = customer_cancellation
        self.ownership_lost = ownership_lost
        self.comments: list[GitHubComment] = []
        self.create_calls = 0

    async def get_comment(self, *args: object, **kwargs: object) -> GitHubComment | None:
        del args, kwargs
        return None

    async def find_comments_by_marker(
        self,
        repository: str,
        issue_number: int,
        marker: str,
        *,
        correlation_id: str,
    ) -> CommentSearchResult:
        del repository, issue_number, correlation_id
        return CommentSearchResult(
            comments=tuple(comment for comment in self.comments if marker in comment.body),
            complete=True,
        )

    async def create_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
        *,
        correlation_id: str,
    ) -> GitHubComment:
        del repository, issue_number, correlation_id
        self.create_calls += 1
        comment = GitHubComment(
            identifier="confirmed-123",
            body=body,
            url="https://github.example/comments/confirmed-123",
            updated_at=datetime.now(UTC),
        )
        self.comments.append(comment)
        if self.customer_cancellation is not None:
            self.customer_cancellation.set()
        if self.ownership_lost is not None:
            self.ownership_lost.set()
        return comment


def _task_execution(claim: ClaimedTask) -> TaskExecution:
    return TaskExecution(
        task_id=claim.task_id,
        task_type=claim.task_type,
        task_input=claim.task_input,
        attempt_id=claim.attempt_id,
        attempt_number=claim.attempt_number,
        max_attempts=claim.max_attempts,
        execution_fence=claim.execution_fence,
    )


def _handler_context(
    claim: ClaimedTask,
    *,
    customer_cancellation: asyncio.Event,
    ownership_lost: asyncio.Event,
) -> HandlerContext:
    return HandlerContext(
        task_id=claim.task_id,
        attempt_id=claim.attempt_id,
        attempt_number=claim.attempt_number,
        worker_id=claim.worker_id,
        customer_cancellation=customer_cancellation,
        ownership_lost=ownership_lost,
    )


def _integration_handler(
    *,
    settings: Settings,
    github: _AcceptedCommentGitHub,
    actions: ExternalActionRepository,
) -> DeploymentContextSyncHandler:
    return DeploymentContextSyncHandler(
        settings=settings,
        service_catalog=cast(Any, object()),
        jira=cast(Any, object()),
        github=cast(Any, github),
        artifacts=cast(Any, object()),
        actions=actions,
    )


def _decision() -> DeploymentContextDecision:
    return DeploymentContextDecision(
        outcome=DecisionOutcome.READY,
        reason_codes=(DecisionReasonCode.READY,),
        reasons=("Ready.",),
        source_references=(),
        policy_version=7,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
    )


async def _expire_lease(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(lease_expires_at=text("clock_timestamp() - interval '1 second'"))
        )


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


def test_customer_cancellation_after_confirmed_write_preserves_action_success(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, claim = await _claim(
            postgres_database_url,
            idempotency_key=f"cancel-after-write-{uuid4()}",
            worker_id="cancellation-worker",
        )
        try:
            settings = Settings(
                database_url=postgres_database_url,
                environment="test",
                integration_settlement_delay_seconds=0.1,
            )
            actions = ExternalActionRepository(session_factory)
            scope = "deployment_context_sync:v1:test:424242:42:payments-api:github_comment"
            body = f"{safe_marker(scope)}\nbounded comment"
            fingerprint = sha256_json({"body": body})
            reservation = await actions.reserve_or_get(
                fence=claim.execution_fence,
                action_scope_key=scope,
                desired_request_fingerprint=fingerprint,
                decision_snapshot_hash="b" * 64,
            )
            customer_cancellation = asyncio.Event()
            github = _AcceptedCommentGitHub(customer_cancellation=customer_cancellation)
            handler = _integration_handler(settings=settings, github=github, actions=actions)

            with pytest.raises(TaskCancelled):
                await handler._reconcile_and_apply(
                    task=_task_execution(claim),
                    context=_handler_context(
                        claim,
                        customer_cancellation=customer_cancellation,
                        ownership_lost=asyncio.Event(),
                    ),
                    action=reservation.action,
                    repository="customer/example-service",
                    issue_number=42,
                    body=body,
                    desired_fingerprint=fingerprint,
                    decision=_decision(),
                    references=(),
                )

            assert await WorkerRepository(session_factory).finalize_cancellation(claim) is (
                FenceResult.APPLIED
            )
            persisted_action = await actions.get(reservation.action.id)
            async with session_factory() as session:
                task = await session.get(Task, claim.task_id)
                assert task is not None
            assert github.create_calls == 1
            assert len(github.comments) == 1
            assert persisted_action.status == ExternalActionStatus.SUCCEEDED.value
            assert persisted_action.provider_resource_identifier == "confirmed-123"
            assert persisted_action.completed_at is not None
            assert task.status == TaskStatus.CANCELLED
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ownership_loss_after_provider_success_requires_replacement_reconciliation(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, first = await _claim(
            postgres_database_url,
            idempotency_key=f"ownership-after-write-{uuid4()}",
            worker_id="stale-worker",
        )
        try:
            settings = Settings(
                database_url=postgres_database_url,
                environment="test",
                integration_settlement_delay_seconds=0.1,
            )
            actions = ExternalActionRepository(session_factory)
            worker = WorkerRepository(session_factory)
            scope = "deployment_context_sync:v1:test:424242:42:payments-api:github_comment"
            body = f"{safe_marker(scope)}\nbounded comment"
            fingerprint = sha256_json({"body": body})
            reservation = await actions.reserve_or_get(
                fence=first.execution_fence,
                action_scope_key=scope,
                desired_request_fingerprint=fingerprint,
                decision_snapshot_hash="b" * 64,
            )
            ownership_lost = asyncio.Event()
            github = _AcceptedCommentGitHub(ownership_lost=ownership_lost)
            handler = _integration_handler(settings=settings, github=github, actions=actions)

            with pytest.raises(OwnershipLostError):
                await handler._reconcile_and_apply(
                    task=_task_execution(first),
                    context=_handler_context(
                        first,
                        customer_cancellation=asyncio.Event(),
                        ownership_lost=ownership_lost,
                    ),
                    action=reservation.action,
                    repository="customer/example-service",
                    issue_number=42,
                    body=body,
                    desired_fingerprint=fingerprint,
                    decision=_decision(),
                    references=(),
                )

            stale_action = await actions.get(reservation.action.id)
            async with session_factory() as session:
                stale_attempt_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(ExternalActionAttempt)
                        .where(ExternalActionAttempt.external_action_id == stale_action.id)
                    )
                ).scalar_one()
            assert stale_action.status == ExternalActionStatus.EXECUTING.value
            assert stale_action.provider_resource_identifier is None
            assert stale_attempt_count == 4

            await _expire_lease(session_factory, first.task_id)
            recovered = await worker.recover_one_expired(backoff_seconds=lambda _: 0)
            assert recovered is not None
            second = await worker.claim_one(
                supported_task_types=("deployment_context_sync",),
                worker_id="replacement-worker",
                lease_duration_seconds=30,
            )
            assert second is not None
            await asyncio.sleep(0.12)
            replacement_action = await actions.get(stale_action.id)
            result = await handler._reconcile_and_apply(
                task=_task_execution(second),
                context=_handler_context(
                    second,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                action=replacement_action,
                repository="customer/example-service",
                issue_number=42,
                body=body,
                desired_fingerprint=fingerprint,
                decision=_decision(),
                references=(),
            )
            assert await worker.finalize_success(second, result) is FenceResult.APPLIED

            persisted_action = await actions.get(stale_action.id)
            async with session_factory() as session:
                final_attempt_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(ExternalActionAttempt)
                        .where(ExternalActionAttempt.external_action_id == stale_action.id)
                    )
                ).scalar_one()
            assert github.create_calls == 1
            assert len(github.comments) == 1
            assert persisted_action.status == ExternalActionStatus.SUCCEEDED.value
            assert persisted_action.provider_resource_identifier == "confirmed-123"
            assert final_attempt_count == stale_attempt_count + 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())
