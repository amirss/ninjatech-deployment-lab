from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.connectors import (
    CommentSearchResult,
    GitHubComment,
    RetryableProviderError,
)
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    ServiceCatalogRecord,
)
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionStatus,
    SourceArtifact,
)
from ninjatech_deployment_lab.integrations.workflow import (
    DeploymentContextSyncHandler,
    ReconciliationNeedsReview,
)
from ninjatech_deployment_lab.worker.domain import ExecutionFence, PermanentTaskError
from ninjatech_deployment_lab.worker.handlers import HandlerContext, TaskExecution


class _Catalog:
    def __init__(self, record: ServiceCatalogRecord) -> None:
        self.calls = 0
        self.record = record

    async def fetch_records(
        self,
        service_id: str,
        *,
        correlation_id: str,
    ) -> tuple[ServiceCatalogRecord, ...]:
        self.calls += 1
        return (self.record,)


class _ForbiddenDownstream:
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        async def forbidden(*args: object, **kwargs: object) -> object:
            self.calls += 1
            raise AssertionError(f"downstream provider was called: {name}")

        return forbidden


class _Artifacts:
    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[dict[str, object]] = []

    async def record(self, **values: Any) -> SourceArtifact:
        self.calls += 1
        payload = values["normalized_payload"]
        self.payloads.append(cast(dict[str, object], payload))
        return SourceArtifact(
            id=uuid4(),
            task_id=values["fence"].task_id,
            observed_by_attempt_id=values["fence"].attempt_id,
            provider=values["provider"],
            resource_type=values["resource_type"],
            provider_resource_identifier=values["provider_resource_identifier"],
            canonical_source_url=values["source_url"],
            source_version=values["source_version"],
            schema_version=1,
            data_classification=values["data_classification"].value,
            redaction_applied=values["redaction_applied"],
            retention_until=datetime.now(UTC) + timedelta(days=1),
            normalized_payload=payload,
            content_hash="a" * 64,
            payload_size_bytes=100,
            fetched_at=values["fetched_at"],
            created_at=datetime.now(UTC),
        )


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        environment="test",
        enable_deployment_context_sync=True,
        deployment_scope_id="test-scope",
        deployment_allowed_service_ids=("blocked-service",),
        deployment_allowed_github_repositories=("customer/example-service",),
        deployment_allowed_jira_projects=("ENG",),
    )


def _record() -> ServiceCatalogRecord:
    return ServiceCatalogRecord(
        service_id="blocked-service",
        canonical_service_id="blocked-service",
        service_owner="team-security",
        criticality="tier-1",
        approved_repositories=("customer/example-service",),
        data_classification=DataClassification.RESTRICTED,
        deployment_policy_version=7,
        automatic_publication_allowed=True,
        allow_automatic_updates=False,
        source_version="v7",
        source_url="https://catalog.example/services/blocked-service",
    )


def _execution() -> tuple[TaskExecution, HandlerContext]:
    task_id = uuid4()
    attempt_id = uuid4()
    fence = ExecutionFence(
        task_id=task_id,
        attempt_id=attempt_id,
        attempt_number=1,
        worker_id="worker",
        lease_token_hash="a" * 64,
    )
    return (
        TaskExecution(
            task_id=task_id,
            task_type="deployment_context_sync",
            task_input={
                "jira_issue_key": "ENG-123",
                "github_repository": "customer/example-service",
                "github_issue_number": 42,
                "service_id": "blocked-service",
                "publish_slack_notification": False,
            },
            attempt_id=attempt_id,
            attempt_number=1,
            max_attempts=3,
            execution_fence=fence,
        ),
        HandlerContext(
            task_id=task_id,
            attempt_id=attempt_id,
            attempt_number=1,
            worker_id="worker",
            customer_cancellation=asyncio.Event(),
            ownership_lost=asyncio.Event(),
        ),
    )


def test_catalog_policy_blocks_before_jira_github_or_action() -> None:
    catalog = _Catalog(_record())
    downstream = _ForbiddenDownstream()
    artifacts = _Artifacts()
    handler = DeploymentContextSyncHandler(
        settings=_settings(),
        service_catalog=catalog,
        jira=cast(Any, downstream),
        github=cast(Any, downstream),
        artifacts=cast(Any, artifacts),
        actions=cast(Any, downstream),
    )
    task, context = _execution()

    result = asyncio.run(handler.execute(task, context))

    decision = cast(dict[str, object], result["decision"])
    assert decision["outcome"] == "blocked"
    assert catalog.calls == 1
    assert artifacts.calls == 1
    assert downstream.calls == 0
    assert "service_owner" not in artifacts.payloads[0]
    assert "approved_repositories" not in artifacts.payloads[0]
    artifact_list = cast(list[object], result["source_artifacts"])
    artifact = cast(dict[str, object], artifact_list[0])
    assert artifact["provider"] == "service_catalog"


def test_checkpoint_4a_does_not_silently_accept_slack_publication() -> None:
    catalog = _Catalog(_record())
    downstream = _ForbiddenDownstream()
    handler = DeploymentContextSyncHandler(
        settings=_settings(),
        service_catalog=catalog,
        jira=cast(Any, downstream),
        github=cast(Any, downstream),
        artifacts=cast(Any, _Artifacts()),
        actions=cast(Any, downstream),
    )
    task, context = _execution()
    task = TaskExecution(
        task_id=task.task_id,
        task_type=task.task_type,
        task_input={
            **task.task_input,
            "publish_slack_notification": True,
            "slack_channel_id": "C1234567890",
        },
        attempt_id=task.attempt_id,
        attempt_number=task.attempt_number,
        max_attempts=task.max_attempts,
        execution_fence=task.execution_fence,
    )
    with pytest.raises(PermanentTaskError) as captured:
        asyncio.run(handler.execute(task, context))
    assert captured.value.error_code == "slack_checkpoint_not_enabled"
    assert catalog.calls == 0


class _ReconciliationGitHub:
    def __init__(
        self,
        *,
        exact: GitHubComment | None = None,
        search: CommentSearchResult | None = None,
        read_failure: bool = False,
    ) -> None:
        self.exact = exact
        self.search = search
        self.read_failure = read_failure

    async def get_comment(self, *args: object, **kwargs: object) -> GitHubComment | None:
        return self.exact

    async def find_comments_by_marker(
        self,
        *args: object,
        **kwargs: object,
    ) -> CommentSearchResult:
        if self.read_failure:
            raise RetryableProviderError("github_reconciliation_unavailable")
        assert self.search is not None
        return self.search


def _action(*, provider_identifier: str | None) -> ExternalAction:
    now = datetime.now(UTC)
    return ExternalAction(
        id=uuid4(),
        task_id=uuid4(),
        current_attempt_id=uuid4(),
        provider="github",
        operation="upsert_deployment_context_comment",
        action_scope_key=("deployment_context_sync:v1:test:424242:42:payments-api:github_comment"),
        desired_request_fingerprint="a" * 64,
        decision_snapshot_hash="b" * 64,
        revision=1,
        status=ExternalActionStatus.RECONCILING.value,
        provider_resource_identifier=provider_identifier,
        action_attempt_count=2,
        reserved_at=now,
        last_attempt_at=now,
        created_at=now,
        updated_at=now,
    )


def _reconciliation_handler(github: _ReconciliationGitHub) -> DeploymentContextSyncHandler:
    forbidden = _ForbiddenDownstream()
    return DeploymentContextSyncHandler(
        settings=_settings(),
        service_catalog=cast(Any, forbidden),
        jira=cast(Any, forbidden),
        github=cast(Any, github),
        artifacts=cast(Any, forbidden),
        actions=cast(Any, forbidden),
    )


def test_known_deleted_comment_is_not_recreated() -> None:
    handler = _reconciliation_handler(_ReconciliationGitHub(exact=None))
    action = _action(provider_identifier="100")
    try:
        asyncio.run(
            handler._reconcile_comment(
                action=action,
                repository="customer/example-service",
                issue_number=42,
                marker="marker",
                correlation_id="test",
            )
        )
    except ReconciliationNeedsReview as error:
        assert error.code.value == "comment_missing"
    else:
        raise AssertionError("deleted comment did not require human review")


def test_multiple_markers_require_review() -> None:
    now = datetime.now(UTC)
    comments = tuple(
        GitHubComment(
            identifier=str(index),
            body="marker",
            url=f"https://github.example/comments/{index}",
            updated_at=now,
        )
        for index in (1, 2)
    )
    handler = _reconciliation_handler(
        _ReconciliationGitHub(search=CommentSearchResult(comments=comments, complete=True))
    )
    with pytest.raises(ReconciliationNeedsReview, match="requires human review"):
        asyncio.run(
            handler._reconcile_comment(
                action=_action(provider_identifier=None),
                repository="customer/example-service",
                issue_number=42,
                marker="marker",
                correlation_id="test",
            )
        )


def test_read_only_reconciliation_failure_is_retryable_not_outcome_unknown() -> None:
    handler = _reconciliation_handler(_ReconciliationGitHub(read_failure=True))
    with pytest.raises(RetryableProviderError):
        asyncio.run(
            handler._reconcile_comment(
                action=_action(provider_identifier=None),
                repository="customer/example-service",
                issue_number=42,
                marker="marker",
                correlation_id="test",
            )
        )
