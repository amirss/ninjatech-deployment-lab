from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
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
    ExternalActionReference,
    SlackDeliveryState,
    sha256_json,
)
from ninjatech_deployment_lab.integrations.metrics import InMemoryMetricsSink
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionAttempt,
    ExternalActionOperation,
    ExternalActionProvider,
    ExternalActionStatus,
    SourceArtifact,
)
from ninjatech_deployment_lab.integrations.persistence import (
    ActionReservation,
    ActionReservationKind,
    ExternalActionRepository,
    SourceArtifactRepository,
)
from ninjatech_deployment_lab.integrations.slack import (
    SlackDeliveryRequest,
    SlackDeliveryService,
    SlackMessageReceipt,
    SlackMessageRequest,
    SlackOutcomeUnknown,
    SlackRetryableFailure,
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
        after_accept: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.customer_cancellation = customer_cancellation
        self.ownership_lost = ownership_lost
        self.after_accept = after_accept
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
        if self.after_accept is not None:
            await self.after_accept()
        if self.customer_cancellation is not None:
            self.customer_cancellation.set()
        if self.ownership_lost is not None:
            self.ownership_lost.set()
        return comment


class _SlackNotifier:
    def __init__(
        self,
        *,
        outcome: SlackMessageReceipt | Exception,
        after_accept: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.outcome = outcome
        self.after_accept = after_accept
        self.identity_calls = 0
        self.post_calls = 0

    async def verify_identity(self, *, correlation_id: str) -> bool:
        self.identity_calls += 1
        return True

    async def post_notification(
        self,
        request: SlackMessageRequest,
        *,
        correlation_id: str,
    ) -> SlackMessageReceipt:
        self.post_calls += 1
        if self.after_accept is not None:
            await self.after_accept()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


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


def _slack_settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        environment="test",
        enable_deployment_context_sync=True,
        deployment_scope_id="test-scope",
        deployment_allowed_service_ids=("payments-api",),
        deployment_allowed_github_repositories=("customer/example-service",),
        deployment_allowed_jira_projects=("ENG",),
        github_expected_login="test-bot",
        enable_slack_notification=True,
        slack_bot_token=SecretStr("test-only-token"),
        slack_expected_team_id="T1234567890",
        slack_expected_user_id="U1234567890",
        deployment_allowed_slack_channels=("C1234567890",),
    )


def _slack_request() -> SlackDeliveryRequest:
    return SlackDeliveryRequest(
        channel_id="C1234567890",
        canonical_service_id="payments-api",
        github_repository_id=424242,
        github_issue_number=42,
        github_action=ExternalActionReference(
            action_id=uuid4(),
            provider="github",
            operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT.value,
            revision=1,
            provider_resource_identifier="1000",
            provider_url=(
                "https://github.example/customer/example-service/issues/42#issuecomment-1000"
            ),
        ),
        decision=_decision(),
    )


def _slack_delivery(
    *,
    settings: Settings,
    notifier: _SlackNotifier,
    actions: ExternalActionRepository,
) -> SlackDeliveryService:
    return SlackDeliveryService(
        settings=settings,
        notifier=notifier,
        actions=actions,
        metrics=InMemoryMetricsSink(),
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
                    provider=ExternalActionProvider.GITHUB,
                    operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
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


def test_slack_action_scope_is_reserved_once_under_concurrency(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, claim = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-action-{uuid4()}",
            worker_id="slack-action-worker",
        )
        try:
            repository = ExternalActionRepository(session_factory)

            async def reserve() -> ActionReservation:
                return await repository.reserve_or_get(
                    fence=claim.execution_fence,
                    provider=ExternalActionProvider.SLACK,
                    operation=(ExternalActionOperation.POST_DEPLOYMENT_CONTEXT_NOTIFICATION),
                    action_scope_key=(
                        "deployment_context_sync:v1:test:424242:42:payments-api:"
                        "github_revision:1:slack_channel:C1234567890:notification"
                    ),
                    desired_request_fingerprint="c" * 64,
                    decision_snapshot_hash="d" * 64,
                )

            first, second = await asyncio.gather(reserve(), reserve())
            assert {first.kind, second.kind} == {
                ActionReservationKind.CREATED,
                ActionReservationKind.REPLAY,
            }
            changed = await repository.reserve_or_get(
                fence=claim.execution_fence,
                provider=ExternalActionProvider.SLACK,
                operation=ExternalActionOperation.POST_DEPLOYMENT_CONTEXT_NOTIFICATION,
                action_scope_key=(
                    "deployment_context_sync:v1:test:424242:42:payments-api:"
                    "github_revision:1:slack_channel:C1234567890:notification"
                ),
                desired_request_fingerprint="e" * 64,
                decision_snapshot_hash="d" * 64,
            )
            assert changed.kind is ActionReservationKind.CHANGED
            async with session_factory() as session:
                actions = (
                    (
                        await session.execute(
                            select(ExternalAction).where(ExternalAction.provider == "slack")
                        )
                    )
                    .scalars()
                    .all()
                )
                attempts = (
                    (
                        await session.execute(
                            select(ExternalActionAttempt).where(
                                ExternalActionAttempt.external_action_id == actions[0].id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(actions) == 1
            assert len(attempts) == 3
            assert {attempt.sequence_number for attempt in attempts} == {1, 2, 3}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_slack_success_is_reused_by_an_independent_task(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        first_engine, first_factory, first = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-success-first-{uuid4()}",
            worker_id="slack-first",
        )
        second_engine, second_factory, second = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-success-second-{uuid4()}",
            worker_id="slack-second",
        )
        try:
            notifier = _SlackNotifier(
                outcome=SlackMessageReceipt(
                    channel="C1234567890",
                    timestamp="1722000000.000001",
                )
            )
            actions = ExternalActionRepository(first_factory)
            delivery = _slack_delivery(
                settings=_slack_settings(postgres_database_url),
                notifier=notifier,
                actions=actions,
            )
            first_result = await delivery.deliver(
                task=_task_execution(first),
                context=_handler_context(
                    first,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            second_result = await delivery.deliver(
                task=_task_execution(second),
                context=_handler_context(
                    second,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            assert first_result.state is SlackDeliveryState.SUCCEEDED
            assert second_result.state is SlackDeliveryState.SUCCEEDED
            assert first_result.action is not None
            assert second_result.action is not None
            assert first_result.action.action_id == second_result.action.action_id
            assert notifier.identity_calls == 1
            assert notifier.post_calls == 1
            async with second_factory() as session:
                slack_actions = (
                    (
                        await session.execute(
                            select(ExternalAction).where(ExternalAction.provider == "slack")
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(slack_actions) == 1
            assert slack_actions[0].provider_resource_identifier == "C1234567890:1722000000.000001"
        finally:
            await first_engine.dispose()
            await second_engine.dispose()

    asyncio.run(scenario())


def test_slack_unknown_outcome_is_persisted_and_never_reposted(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        first_engine, first_factory, first = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-unknown-first-{uuid4()}",
            worker_id="slack-unknown-first",
        )
        second_engine, _, second = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-unknown-second-{uuid4()}",
            worker_id="slack-unknown-second",
        )
        try:
            notifier = _SlackNotifier(outcome=SlackOutcomeUnknown("slack_write_outcome_unknown"))
            actions = ExternalActionRepository(first_factory)
            delivery = _slack_delivery(
                settings=_slack_settings(postgres_database_url),
                notifier=notifier,
                actions=actions,
            )
            first_result = await delivery.deliver(
                task=_task_execution(first),
                context=_handler_context(
                    first,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            second_result = await delivery.deliver(
                task=_task_execution(second),
                context=_handler_context(
                    second,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            assert first_result.state is SlackDeliveryState.OUTCOME_UNKNOWN
            assert second_result.state is SlackDeliveryState.OUTCOME_UNKNOWN
            assert notifier.identity_calls == 1
            assert notifier.post_calls == 1
            assert first_result.action is not None
            persisted = await actions.get(first_result.action.action_id)
            assert persisted.status == ExternalActionStatus.OUTCOME_UNKNOWN.value
            assert persisted.provider_resource_identifier is None
        finally:
            await first_engine.dispose()
            await second_engine.dispose()

    asyncio.run(scenario())


def test_slack_rate_limit_persists_not_before_and_retries_after_database_time(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        first_engine, first_factory, first = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-retry-first-{uuid4()}",
            worker_id="slack-retry-first",
        )
        second_engine, _, second = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-retry-second-{uuid4()}",
            worker_id="slack-retry-second",
        )
        try:
            actions = ExternalActionRepository(first_factory)
            failed_notifier = _SlackNotifier(
                outcome=SlackRetryableFailure(
                    "slack_rate_limited",
                    retry_after_seconds=30,
                )
            )
            failed = await _slack_delivery(
                settings=_slack_settings(postgres_database_url),
                notifier=failed_notifier,
                actions=actions,
            ).deliver(
                task=_task_execution(first),
                context=_handler_context(
                    first,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            assert failed.state is SlackDeliveryState.RETRYABLE_FAILURE
            assert failed.action is not None
            failed_action = await actions.get(failed.action.action_id)
            assert failed_action.write_started_at is None
            assert failed_action.reconcile_not_before is not None
            assert await actions.reconciliation_delay_seconds(failed_action.id) > 0

            successful_notifier = _SlackNotifier(
                outcome=SlackMessageReceipt(
                    channel="C1234567890",
                    timestamp="1722000000.000002",
                )
            )
            succeeded = await _slack_delivery(
                settings=_slack_settings(postgres_database_url),
                notifier=successful_notifier,
                actions=actions,
            ).deliver(
                task=_task_execution(second),
                context=_handler_context(
                    second,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            assert succeeded.state is SlackDeliveryState.RETRYABLE_FAILURE
            assert failed_notifier.post_calls == 1
            assert successful_notifier.identity_calls == 0
            assert successful_notifier.post_calls == 0

            async with first_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ExternalAction)
                        .where(ExternalAction.id == failed_action.id)
                        .values(
                            reconcile_not_before=(
                                func.clock_timestamp() - text("interval '1 second'")
                            )
                        )
                    )

            later = await _slack_delivery(
                settings=_slack_settings(postgres_database_url),
                notifier=successful_notifier,
                actions=actions,
            ).deliver(
                task=_task_execution(second),
                context=_handler_context(
                    second,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            assert later.state is SlackDeliveryState.SUCCEEDED
            assert later.action is not None
            assert later.action.action_id == failed_action.id
            assert successful_notifier.identity_calls == 1
            assert successful_notifier.post_calls == 1
        finally:
            await first_engine.dispose()
            await second_engine.dispose()

    asyncio.run(scenario())


def test_customer_cancellation_after_slack_success_preserves_ledger_truth(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, claim = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-cancel-{uuid4()}",
            worker_id="slack-cancel-worker",
        )
        cancellation = asyncio.Event()

        async def cancel_after_accept() -> None:
            async with session_factory() as session:
                await TaskService(session).cancel_task(claim.task_id)
            cancellation.set()

        try:
            notifier = _SlackNotifier(
                outcome=SlackMessageReceipt(
                    channel="C1234567890",
                    timestamp="1722000000.000001",
                ),
                after_accept=cancel_after_accept,
            )
            actions = ExternalActionRepository(session_factory)
            with pytest.raises(TaskCancelled):
                await _slack_delivery(
                    settings=_slack_settings(postgres_database_url),
                    notifier=notifier,
                    actions=actions,
                ).deliver(
                    task=_task_execution(claim),
                    context=_handler_context(
                        claim,
                        customer_cancellation=cancellation,
                        ownership_lost=asyncio.Event(),
                    ),
                    request=_slack_request(),
                )
            async with session_factory() as session:
                action = (
                    await session.execute(
                        select(ExternalAction).where(ExternalAction.provider == "slack")
                    )
                ).scalar_one()
            assert action.status == ExternalActionStatus.SUCCEEDED.value
            assert action.provider_resource_identifier == "C1234567890:1722000000.000001"
            assert notifier.post_calls == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_cancellation_before_slack_prevents_reservation_and_post(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        engine, session_factory, claim = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-cancel-before-{uuid4()}",
            worker_id="slack-cancel-before-worker",
        )
        cancellation = asyncio.Event()
        cancellation.set()
        notifier = _SlackNotifier(
            outcome=SlackMessageReceipt(
                channel="C1234567890",
                timestamp="1722000000.000001",
            )
        )
        try:
            with pytest.raises(TaskCancelled):
                await _slack_delivery(
                    settings=_slack_settings(postgres_database_url),
                    notifier=notifier,
                    actions=ExternalActionRepository(session_factory),
                ).deliver(
                    task=_task_execution(claim),
                    context=_handler_context(
                        claim,
                        customer_cancellation=cancellation,
                        ownership_lost=asyncio.Event(),
                    ),
                    request=_slack_request(),
                )
            async with session_factory() as session:
                action_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(ExternalAction)
                        .where(ExternalAction.provider == "slack")
                    )
                ).scalar_one()
            assert action_count == 0
            assert notifier.identity_calls == 0
            assert notifier.post_calls == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ownership_loss_after_slack_acceptance_forces_no_resend(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> None:
        first_engine, first_factory, first = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-owner-first-{uuid4()}",
            worker_id="slack-owner-first",
        )
        second_engine, _, second = await _claim(
            postgres_database_url,
            idempotency_key=f"slack-owner-second-{uuid4()}",
            worker_id="slack-owner-second",
        )
        ownership_lost = asyncio.Event()

        async def lose_ownership() -> None:
            ownership_lost.set()

        try:
            notifier = _SlackNotifier(
                outcome=SlackMessageReceipt(
                    channel="C1234567890",
                    timestamp="1722000000.000001",
                ),
                after_accept=lose_ownership,
            )
            actions = ExternalActionRepository(first_factory)
            delivery = _slack_delivery(
                settings=_slack_settings(postgres_database_url),
                notifier=notifier,
                actions=actions,
            )
            with pytest.raises(OwnershipLostError):
                await delivery.deliver(
                    task=_task_execution(first),
                    context=_handler_context(
                        first,
                        customer_cancellation=asyncio.Event(),
                        ownership_lost=ownership_lost,
                    ),
                    request=_slack_request(),
                )
            async with first_factory() as session:
                action = (
                    await session.execute(
                        select(ExternalAction).where(ExternalAction.provider == "slack")
                    )
                ).scalar_one()
                attempts_before = (
                    await session.execute(
                        select(func.count())
                        .select_from(ExternalActionAttempt)
                        .where(ExternalActionAttempt.external_action_id == action.id)
                    )
                ).scalar_one()
            assert action.status == ExternalActionStatus.EXECUTING.value
            assert action.provider_resource_identifier is None
            assert attempts_before == 2

            replacement_result = await delivery.deliver(
                task=_task_execution(second),
                context=_handler_context(
                    second,
                    customer_cancellation=asyncio.Event(),
                    ownership_lost=asyncio.Event(),
                ),
                request=_slack_request(),
            )
            assert replacement_result.state is SlackDeliveryState.OUTCOME_UNKNOWN
            assert notifier.post_calls == 1
            async with first_factory() as session:
                persisted = await session.get(ExternalAction, action.id)
                attempts_after = (
                    await session.execute(
                        select(func.count())
                        .select_from(ExternalActionAttempt)
                        .where(ExternalActionAttempt.external_action_id == action.id)
                    )
                ).scalar_one()
            assert persisted is not None
            assert persisted.status == ExternalActionStatus.OUTCOME_UNKNOWN.value
            assert attempts_after == 4
        finally:
            await first_engine.dispose()
            await second_engine.dispose()

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
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
                action_scope_key=scope,
                desired_request_fingerprint="a" * 64,
                decision_snapshot_hash="b" * 64,
            )
            replay = await second_repository.reserve_or_get(
                fence=second_claim.execution_fence,
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
                action_scope_key=scope,
                desired_request_fingerprint="a" * 64,
                decision_snapshot_hash="b" * 64,
            )
            drift = await second_repository.reserve_or_get(
                fence=second_claim.execution_fence,
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
                action_scope_key=scope,
                desired_request_fingerprint="a" * 64,
                decision_snapshot_hash="c" * 64,
            )
            changed = await second_repository.reserve_or_get(
                fence=second_claim.execution_fence,
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
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
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
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
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
                action_scope_key=scope,
                desired_request_fingerprint=fingerprint,
                decision_snapshot_hash="b" * 64,
            )
            customer_cancellation = asyncio.Event()

            async def request_customer_cancellation() -> None:
                async with session_factory() as cancellation_session:
                    await TaskService(cancellation_session).cancel_task(claim.task_id)

            github = _AcceptedCommentGitHub(
                customer_cancellation=customer_cancellation,
                after_accept=request_customer_cancellation,
            )
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
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
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
