from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.connectors import (
    CommentSearchResult,
    GitHubComment,
    RetryableProviderError,
    safe_marker,
)
from ninjatech_deployment_lab.integrations.domain import (
    DataClassification,
    DecisionOutcome,
    DecisionReasonCode,
    DeploymentContextDecision,
    GitHubRepositoryContext,
    JiraWorkItem,
    ServiceCatalogRecord,
    SourceReference,
    sha256_json,
)
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionStatus,
    SourceArtifact,
)
from ninjatech_deployment_lab.integrations.workflow import (
    DeploymentContextSyncHandler,
    ReconciliationNeedsReview,
    _action_reference,
)
from ninjatech_deployment_lab.worker.domain import (
    ExecutionFence,
    ExecutionInvariantError,
    OwnershipLostError,
    PermanentTaskError,
    TaskCancelled,
)
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
        github_expected_login="simulator-bot",
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
    assert captured.value.error_code == "slack_notification_not_enabled"
    assert catalog.calls == 0


def _slack_enabled_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        environment="test",
        enable_deployment_context_sync=True,
        deployment_scope_id="test-scope",
        deployment_allowed_service_ids=("blocked-service",),
        deployment_allowed_github_repositories=("customer/example-service",),
        deployment_allowed_jira_projects=("ENG",),
        github_expected_login="simulator-bot",
        enable_slack_notification=True,
        slack_bot_token=SecretStr("test-only-token"),
        slack_expected_team_id="T1234567890",
        slack_expected_user_id="U1234567890",
        deployment_allowed_slack_channels=("C1234567890",),
    )


def test_unauthorized_slack_request_is_rejected_before_catalog_access() -> None:
    catalog = _Catalog(_record())
    downstream = _ForbiddenDownstream()
    handler = DeploymentContextSyncHandler(
        settings=_slack_enabled_settings(),
        service_catalog=catalog,
        jira=cast(Any, downstream),
        github=cast(Any, downstream),
        artifacts=cast(Any, _Artifacts()),
        actions=cast(Any, downstream),
        slack_delivery_factory=lambda: cast(Any, downstream),
    )
    task, context = _execution()
    task = TaskExecution(
        task_id=task.task_id,
        task_type=task.task_type,
        task_input={
            **task.task_input,
            "publish_slack_notification": True,
            "slack_channel_id": "C0000000000",
        },
        attempt_id=task.attempt_id,
        attempt_number=task.attempt_number,
        max_attempts=task.max_attempts,
        execution_fence=task.execution_fence,
    )
    with pytest.raises(PermanentTaskError) as captured:
        asyncio.run(handler.execute(task, context))
    assert captured.value.error_code == "slack_channel_not_allowed"
    assert catalog.calls == 0
    assert downstream.calls == 0


def test_policy_blocked_slack_request_performs_no_slack_or_downstream_access() -> None:
    catalog = _Catalog(_record())
    downstream = _ForbiddenDownstream()
    factory_calls = 0

    def forbidden_factory() -> Any:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("Slack connector was constructed for blocked policy")

    handler = DeploymentContextSyncHandler(
        settings=_slack_enabled_settings(),
        service_catalog=catalog,
        jira=cast(Any, downstream),
        github=cast(Any, downstream),
        artifacts=cast(Any, _Artifacts()),
        actions=cast(Any, downstream),
        slack_delivery_factory=forbidden_factory,
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
    result = asyncio.run(handler.execute(task, context))
    notification = cast(dict[str, object], result["secondary_slack_notification"])
    assert notification["requested"] is True
    assert notification["state"] == "needs_human_review"
    assert catalog.calls == 1
    assert downstream.calls == 0
    assert factory_calls == 0


def _ready_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        environment="test",
        enable_deployment_context_sync=True,
        deployment_scope_id="test-scope",
        deployment_allowed_service_ids=("payments-api",),
        deployment_allowed_github_repositories=("customer/example-service",),
        deployment_allowed_jira_projects=("ENG",),
        deployment_minimum_policy_version=7,
        github_expected_login="simulator-bot",
    )


def _ready_record() -> ServiceCatalogRecord:
    return ServiceCatalogRecord(
        service_id="payments-api",
        canonical_service_id="payments-api",
        service_owner="team-payments",
        criticality="tier-1",
        approved_repositories=("customer/example-service",),
        data_classification=DataClassification.INTERNAL,
        deployment_policy_version=7,
        automatic_publication_allowed=True,
        allow_automatic_updates=False,
        source_version="v7",
        source_url="https://catalog.example/services/payments-api",
    )


def _github_context(**changes: object) -> GitHubRepositoryContext:
    values: dict[str, object] = {
        "full_name": "customer/example-service",
        "repository_id": 424242,
        "visibility": "private",
        "archived": False,
        "default_branch": "main",
        "default_branch_head_sha": "a" * 40,
        "issue_number": 42,
        "issue_state": "open",
        "issue_title": "Deployment context",
        "is_pull_request": False,
        "source_url": "https://github.example/customer/example-service/issues/42",
        "source_version": "github-v1",
    }
    values.update(changes)
    return GitHubRepositoryContext.model_validate(values)


def _jira_item(**changes: object) -> JiraWorkItem:
    values: dict[str, object] = {
        "key": "ENG-123",
        "title": "Deployment request",
        "normalized_description_text": "bounded description",
        "status": "Open",
        "updated_at": datetime(2026, 7, 28, tzinfo=UTC),
        "source_url": "https://jira.example/browse/ENG-123",
        "source_version": "jira-v1",
    }
    values.update(changes)
    return JiraWorkItem.model_validate(values)


def _ready_execution() -> tuple[TaskExecution, HandlerContext]:
    task, context = _execution()
    return (
        TaskExecution(
            task_id=task.task_id,
            task_type=task.task_type,
            task_input={
                **task.task_input,
                "service_id": "payments-api",
            },
            attempt_id=task.attempt_id,
            attempt_number=task.attempt_number,
            max_attempts=task.max_attempts,
            execution_fence=task.execution_fence,
        ),
        context,
    )


class _GitHubContextReader:
    def __init__(
        self,
        context: GitHubRepositoryContext,
        *,
        identity_verified: bool = True,
    ) -> None:
        self.context = context
        self.identity_verified = identity_verified
        self.identity_calls = 0
        self.fetch_calls = 0
        self.write_calls = 0

    async def verify_identity(self, *, correlation_id: str) -> bool:
        del correlation_id
        self.identity_calls += 1
        return self.identity_verified

    async def fetch_context(
        self,
        repository: str,
        issue_number: int,
        *,
        correlation_id: str,
    ) -> GitHubRepositoryContext:
        del repository, issue_number, correlation_id
        self.fetch_calls += 1
        return self.context

    async def create_comment(self, *args: object, **kwargs: object) -> GitHubComment:
        del args, kwargs
        self.write_calls += 1
        raise AssertionError("identity-rejected workflow attempted a GitHub write")

    async def update_comment(self, *args: object, **kwargs: object) -> GitHubComment:
        del args, kwargs
        self.write_calls += 1
        raise AssertionError("identity-rejected workflow attempted a GitHub write")


class _JiraReader:
    def __init__(self, item: JiraWorkItem) -> None:
        self.item = item
        self.calls = 0

    async def fetch_issue(self, issue_key: str, *, correlation_id: str) -> JiraWorkItem:
        del issue_key, correlation_id
        self.calls += 1
        return self.item


@pytest.mark.parametrize(
    ("github", "reason_code", "expected_outcome"),
    [
        (
            _github_context(full_name="another/repository"),
            DecisionReasonCode.GITHUB_REPOSITORY_IDENTITY_MISMATCH,
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
        ),
        (
            _github_context(issue_number=99),
            DecisionReasonCode.GITHUB_ISSUE_IDENTITY_MISMATCH,
            DecisionOutcome.NEEDS_HUMAN_REVIEW,
        ),
        (
            _github_context(is_pull_request=True),
            DecisionReasonCode.TARGET_IS_PULL_REQUEST,
            DecisionOutcome.BLOCKED,
        ),
    ],
)
def test_github_target_identity_failures_produce_no_write(
    github: GitHubRepositoryContext,
    reason_code: DecisionReasonCode,
    expected_outcome: DecisionOutcome,
) -> None:
    github_reader = _GitHubContextReader(github)
    jira = _JiraReader(_jira_item())
    actions = _ForbiddenDownstream()
    handler = DeploymentContextSyncHandler(
        settings=_ready_settings(),
        service_catalog=_Catalog(_ready_record()),
        jira=cast(Any, jira),
        github=cast(Any, github_reader),
        artifacts=cast(Any, _Artifacts()),
        actions=cast(Any, actions),
    )
    task, context = _ready_execution()

    result = asyncio.run(handler.execute(task, context))

    decision = cast(dict[str, object], result["decision"])
    assert decision["outcome"] == expected_outcome.value
    assert decision["reason_codes"] == [reason_code.value]
    assert github_reader.write_calls == 0
    assert jira.calls == 0
    assert actions.calls == 0


def test_jira_identity_mismatch_produces_no_github_write() -> None:
    github_reader = _GitHubContextReader(_github_context())
    actions = _ForbiddenDownstream()
    handler = DeploymentContextSyncHandler(
        settings=_ready_settings(),
        service_catalog=_Catalog(_ready_record()),
        jira=cast(Any, _JiraReader(_jira_item(key="ENG-999"))),
        github=cast(Any, github_reader),
        artifacts=cast(Any, _Artifacts()),
        actions=cast(Any, actions),
    )
    task, context = _ready_execution()

    result = asyncio.run(handler.execute(task, context))

    decision = cast(dict[str, object], result["decision"])
    assert decision["reason_codes"] == [DecisionReasonCode.JIRA_ISSUE_IDENTITY_MISMATCH.value]
    assert github_reader.write_calls == 0
    assert actions.calls == 0


def test_wrong_github_principal_stops_before_context_fetch_or_write() -> None:
    github_reader = _GitHubContextReader(_github_context(), identity_verified=False)
    jira = _JiraReader(_jira_item())
    actions = _ForbiddenDownstream()
    handler = DeploymentContextSyncHandler(
        settings=_ready_settings(),
        service_catalog=_Catalog(_ready_record()),
        jira=cast(Any, jira),
        github=cast(Any, github_reader),
        artifacts=cast(Any, _Artifacts()),
        actions=cast(Any, actions),
    )
    task, context = _ready_execution()

    result = asyncio.run(handler.execute(task, context))

    decision = cast(dict[str, object], result["decision"])
    assert decision["reason_codes"] == [DecisionReasonCode.PROVIDER_IDENTITY_UNVERIFIED.value]
    assert github_reader.fetch_calls == 0
    assert github_reader.write_calls == 0
    assert jira.calls == 0
    assert actions.calls == 0


def test_correct_github_principal_allows_context_evaluation() -> None:
    github_reader = _GitHubContextReader(_github_context(archived=True))
    handler = DeploymentContextSyncHandler(
        settings=_ready_settings(),
        service_catalog=_Catalog(_ready_record()),
        jira=cast(Any, _ForbiddenDownstream()),
        github=cast(Any, github_reader),
        artifacts=cast(Any, _Artifacts()),
        actions=cast(Any, _ForbiddenDownstream()),
    )
    task, context = _ready_execution()

    result = asyncio.run(handler.execute(task, context))

    decision = cast(dict[str, object], result["decision"])
    assert decision["reason_codes"] == [DecisionReasonCode.REPOSITORY_ARCHIVED.value]
    assert github_reader.identity_calls == 1
    assert github_reader.fetch_calls == 1
    assert github_reader.write_calls == 0


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


def _source_reference(
    provider: str,
    *,
    source_version: str = "v1",
    content_hash: str = "a" * 64,
) -> SourceReference:
    return SourceReference(
        artifact_id=uuid4(),
        provider=provider,
        resource_type="issue" if provider != "service_catalog" else "service",
        provider_resource_identifier=f"{provider}-resource",
        source_version=source_version,
        content_hash=content_hash,
    )


def _snapshot_decision(
    references: tuple[SourceReference, ...],
) -> DeploymentContextDecision:
    return DeploymentContextDecision(
        outcome=DecisionOutcome.READY,
        reason_codes=(DecisionReasonCode.READY,),
        reasons=("Ready.",),
        source_references=references,
        policy_version=7,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
    )


def test_decision_snapshot_is_order_independent_but_evidence_sensitive() -> None:
    catalog = _source_reference("service_catalog")
    github = _source_reference("github")
    jira = _source_reference("jira")
    baseline = DeploymentContextSyncHandler._decision_snapshot_hash(
        _snapshot_decision((catalog, github, jira))
    )
    reordered = DeploymentContextSyncHandler._decision_snapshot_hash(
        _snapshot_decision((jira, catalog, github))
    )
    changed_version = DeploymentContextSyncHandler._decision_snapshot_hash(
        _snapshot_decision((catalog, github.model_copy(update={"source_version": "v2"}), jira))
    )
    changed_content = DeploymentContextSyncHandler._decision_snapshot_hash(
        _snapshot_decision((catalog, github.model_copy(update={"content_hash": "b" * 64}), jira))
    )
    removed = DeploymentContextSyncHandler._decision_snapshot_hash(
        _snapshot_decision((catalog, github))
    )
    added = DeploymentContextSyncHandler._decision_snapshot_hash(
        _snapshot_decision((catalog, github, jira, _source_reference("extra")))
    )

    assert reordered == baseline
    assert changed_version != baseline
    assert changed_content != baseline
    assert removed != baseline
    assert added != baseline


def test_unconfirmed_action_reference_uses_json_null_not_a_fabricated_identifier() -> None:
    action = _action(provider_identifier=None)
    action.status = ExternalActionStatus.NEEDS_HUMAN_REVIEW.value
    action.provider_url = "https://github.example/comments/not-confirmed"

    payload = _action_reference(action).model_dump(mode="json")

    assert payload["provider_resource_identifier"] is None
    assert payload["provider_url"] is None
    assert "unconfirmed" not in json.dumps(payload)


def test_successful_action_reference_exposes_confirmed_provider_identifier() -> None:
    action = _action(provider_identifier="12345")
    action.status = ExternalActionStatus.SUCCEEDED.value
    action.provider_url = "https://github.example/comments/12345"

    payload = _action_reference(action).model_dump(mode="json")

    assert payload["provider_resource_identifier"] == "12345"
    assert payload["provider_url"] == "https://github.example/comments/12345"


def test_successful_action_reference_rejects_missing_provider_identifier() -> None:
    action = _action(provider_identifier=None)
    action.status = ExternalActionStatus.SUCCEEDED.value

    with pytest.raises(ExecutionInvariantError):
        _action_reference(action)


class _MemoryActions:
    def __init__(self, action: ExternalAction) -> None:
        self.action = action
        self.transitions: list[str] = []

    async def transition(
        self,
        *,
        expected_statuses: set[ExternalActionStatus],
        new_status: ExternalActionStatus,
        transition: str,
        values: dict[str, object] | None = None,
        **kwargs: object,
    ) -> ExternalAction:
        del kwargs
        assert ExternalActionStatus(self.action.status) in expected_statuses
        self.action.status = new_status.value
        self.action.action_attempt_count += 1
        for name, value in (values or {}).items():
            setattr(self.action, name, value)
        self.transitions.append(transition)
        return self.action

    async def reconciliation_delay_seconds(self, action_id: object) -> float:
        del action_id
        return 0.0


class _WriteAwareGitHub:
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


def _execution_with_events() -> tuple[
    TaskExecution,
    HandlerContext,
    asyncio.Event,
    asyncio.Event,
]:
    task, _ = _ready_execution()
    customer_cancellation = asyncio.Event()
    ownership_lost = asyncio.Event()
    return (
        task,
        HandlerContext(
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            attempt_number=task.attempt_number,
            worker_id="worker",
            customer_cancellation=customer_cancellation,
            ownership_lost=ownership_lost,
        ),
        customer_cancellation,
        ownership_lost,
    )


def _action_execution_handler(
    github: _WriteAwareGitHub,
    actions: _MemoryActions,
) -> DeploymentContextSyncHandler:
    forbidden = _ForbiddenDownstream()
    return DeploymentContextSyncHandler(
        settings=_ready_settings(),
        service_catalog=cast(Any, forbidden),
        jira=cast(Any, forbidden),
        github=cast(Any, github),
        artifacts=cast(Any, forbidden),
        actions=cast(Any, actions),
    )


def _run_action(
    handler: DeploymentContextSyncHandler,
    *,
    task: TaskExecution,
    context: HandlerContext,
    action: ExternalAction,
    body: str,
) -> dict[str, object]:
    decision = _snapshot_decision(())
    return cast(
        dict[str, object],
        asyncio.run(
            handler._reconcile_and_apply(
                task=task,
                context=context,
                action=action,
                repository="customer/example-service",
                issue_number=42,
                body=body,
                desired_fingerprint=sha256_json({"body": body}),
                decision=decision,
                references=(),
            )
        ),
    )


def test_customer_cancellation_after_confirmed_write_preserves_external_success() -> None:
    task, context, customer_cancellation, _ = _execution_with_events()
    action = _action(provider_identifier=None)
    action.status = ExternalActionStatus.RESERVED.value
    actions = _MemoryActions(action)
    github = _WriteAwareGitHub(customer_cancellation=customer_cancellation)
    handler = _action_execution_handler(github, actions)
    body = f"{safe_marker(action.action_scope_key)}\nbounded comment"

    with pytest.raises(TaskCancelled):
        _run_action(
            handler,
            task=task,
            context=context,
            action=action,
            body=body,
        )

    assert github.create_calls == 1
    assert len(github.comments) == 1
    assert action.status == ExternalActionStatus.SUCCEEDED.value
    assert action.provider_resource_identifier == "confirmed-123"
    assert actions.transitions[-1] == "write_succeeded"


def test_ownership_loss_after_provider_acceptance_reconciles_without_duplicate() -> None:
    task, context, _, ownership_lost = _execution_with_events()
    action = _action(provider_identifier=None)
    action.status = ExternalActionStatus.RESERVED.value
    actions = _MemoryActions(action)
    github = _WriteAwareGitHub(ownership_lost=ownership_lost)
    handler = _action_execution_handler(github, actions)
    body = f"{safe_marker(action.action_scope_key)}\nbounded comment"

    with pytest.raises(OwnershipLostError):
        _run_action(
            handler,
            task=task,
            context=context,
            action=action,
            body=body,
        )
    stale_transition_count = len(actions.transitions)

    assert action.status == ExternalActionStatus.EXECUTING.value
    assert actions.transitions[-1] == "write_started"
    assert github.create_calls == 1
    assert len(github.comments) == 1

    replacement_task, replacement_context, _, _ = _execution_with_events()
    result = _run_action(
        handler,
        task=replacement_task,
        context=replacement_context,
        action=action,
        body=body,
    )

    assert len(actions.transitions) == stale_transition_count + 2
    assert action.status == ExternalActionStatus.SUCCEEDED.value
    assert action.provider_resource_identifier == "confirmed-123"
    assert result["authoritative_github_action"] is not None
    assert github.create_calls == 1
    assert len(github.comments) == 1


def test_customer_cancellation_before_write_produces_no_provider_action() -> None:
    task, context, customer_cancellation, _ = _execution_with_events()
    customer_cancellation.set()
    action = _action(provider_identifier=None)
    action.status = ExternalActionStatus.RESERVED.value
    actions = _MemoryActions(action)
    github = _WriteAwareGitHub()
    handler = _action_execution_handler(github, actions)
    body = f"{safe_marker(action.action_scope_key)}\nbounded comment"

    with pytest.raises(TaskCancelled):
        _run_action(
            handler,
            task=task,
            context=context,
            action=action,
            body=body,
        )

    assert github.create_calls == 0
    assert github.comments == []
