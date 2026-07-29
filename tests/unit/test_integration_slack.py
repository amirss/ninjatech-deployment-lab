from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.credentials import EnvironmentOrFileCredential
from ninjatech_deployment_lab.integrations.domain import (
    DecisionOutcome,
    DecisionReasonCode,
    DeploymentContextDecision,
    ExternalActionReference,
    SlackDeliveryState,
    SourceReference,
)
from ninjatech_deployment_lab.integrations.http import JsonHttpResponse
from ninjatech_deployment_lab.integrations.metrics import (
    InMemoryMetricsSink,
    MetricLabel,
    MetricName,
    MetricOperation,
    MetricProvider,
    StructuredLoggingMetricsSink,
)
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionStatus,
)
from ninjatech_deployment_lab.integrations.persistence import (
    ActionReservation,
    ActionReservationKind,
)
from ninjatech_deployment_lab.integrations.slack import (
    SlackClient,
    SlackDeliveryRequest,
    SlackDeliveryService,
    SlackMessageReceipt,
    SlackMessageRequest,
    SlackOutcomeUnknown,
    SlackPermanentFailure,
    SlackRetryableFailure,
    render_slack_notification,
    slack_action_scope_key,
    slack_decision_snapshot_hash,
)
from ninjatech_deployment_lab.worker.domain import (
    ExecutionFence,
    OwnershipLostError,
    TaskCancelled,
)
from ninjatech_deployment_lab.worker.handlers import HandlerContext, TaskExecution


class _Http:
    def __init__(self, responses: list[JsonHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request_json(self, **kwargs: Any) -> JsonHttpResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://user:pass@localhost/test",
        "environment": "test",
        "enable_deployment_context_sync": True,
        "deployment_scope_id": "test-scope",
        "deployment_allowed_service_ids": ("payments-api",),
        "deployment_allowed_github_repositories": ("customer/example-service",),
        "deployment_allowed_jira_projects": ("ENG",),
        "github_expected_login": "test-bot",
        "enable_slack_notification": True,
        "slack_bot_token": SecretStr("test-only-token"),
        "slack_expected_team_id": "T1234567890",
        "slack_expected_user_id": "U1234567890",
        "slack_expected_bot_id": "B1234567890",
        "deployment_allowed_slack_channels": ("C1234567890",),
    }
    values.update(updates)
    return Settings(**values)


def _response(
    payload: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> JsonHttpResponse:
    return JsonHttpResponse(
        status_code=status,
        headers=headers or {"content-type": "application/json"},
        payload=cast(Any, payload),
    )


def _auth_payload(
    *,
    team_id: str = "T1234567890",
    user_id: str = "U1234567890",
    bot_id: str = "B1234567890",
) -> dict[str, object]:
    return {
        "ok": True,
        "team_id": team_id,
        "user_id": user_id,
        "bot_id": bot_id,
    }


def _authorized_http(*responses: JsonHttpResponse) -> _Http:
    return _Http([_response(_auth_payload()), *responses])


def _client(http: _Http, settings: Settings | None = None) -> SlackClient:
    configured = settings or _settings()
    return SlackClient(
        http=cast(Any, http),
        base_url=configured.slack_base_url,
        credential=EnvironmentOrFileCredential(
            value=configured.slack_bot_token,
            path=None,
        ),
        settings=configured,
    )


async def _post_with_identity(
    client: SlackClient,
    request: SlackMessageRequest,
) -> SlackMessageReceipt:
    assert await client.verify_identity(correlation_id="request") is True
    return await client.post_notification(request, correlation_id="request")


def _decision() -> DeploymentContextDecision:
    return DeploymentContextDecision(
        outcome=DecisionOutcome.READY,
        reason_codes=(DecisionReasonCode.READY,),
        reasons=("Policy permits publication.",),
        source_references=(
            SourceReference(
                artifact_id=uuid4(),
                provider="github",
                resource_type="repository_issue_context",
                provider_resource_identifier="424242:issue:42",
                source_version="head-a",
                content_hash="a" * 64,
            ),
            SourceReference(
                artifact_id=uuid4(),
                provider="jira",
                resource_type="work_item",
                provider_resource_identifier="ENG-123",
                source_version="jira-v1",
                content_hash="b" * 64,
            ),
        ),
        policy_version=7,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
    )


def _github_action(*, revision: int = 1) -> ExternalActionReference:
    return ExternalActionReference(
        action_id=uuid4(),
        provider="github",
        operation="upsert_deployment_context_comment",
        revision=revision,
        provider_resource_identifier="1000",
        provider_url=(
            "https://github.example/customer/example-service/issues/42#issuecomment-1000"
        ),
    )


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"slack_expected_team_id": None}, "slack_expected_team_id"),
        ({"slack_expected_user_id": None}, "slack_expected_user_id"),
        ({"deployment_allowed_slack_channels": ()}, "Slack channel"),
        ({"slack_bot_token": None}, "bot-token"),
    ],
)
def test_enabled_slack_requires_bounded_trusted_configuration(
    updates: dict[str, Any],
    expected: str,
) -> None:
    with pytest.raises(ValidationError) as captured:
        _settings(**updates)
    assert expected.casefold() in str(captured.value).casefold()


def test_disabled_slack_does_not_require_credentials() -> None:
    configured = _settings(
        enable_slack_notification=False,
        slack_bot_token=None,
        slack_expected_team_id=None,
        slack_expected_user_id=None,
        slack_expected_bot_id=None,
        deployment_allowed_slack_channels=(),
    )
    assert configured.enable_slack_notification is False


def test_slack_mounted_credential_path_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        _settings(
            slack_bot_token=None,
            slack_bot_token_file="relative/slack-token",
        )


def test_slack_identity_uses_exact_opaque_ids_and_is_cached() -> None:
    http = _Http([_response(_auth_payload())])
    client = _client(http)
    assert asyncio.run(client.verify_identity(correlation_id="request-1")) is True
    assert asyncio.run(client.verify_identity(correlation_id="request-2")) is True
    assert len(http.calls) == 1
    assert http.calls[0]["operation"] is MetricOperation.IDENTITY


def test_slack_identity_cache_tracks_rotated_mounted_credential(
    tmp_path: Path,
) -> None:
    credential_path = tmp_path / "slack-token"
    credential_path.write_text("token-a", encoding="utf-8")
    configured = _settings(slack_bot_token=None, slack_bot_token_file=credential_path)
    http = _Http(
        [
            _response(_auth_payload()),
            _response(_auth_payload(team_id="T0000000000")),
        ]
    )
    client = SlackClient(
        http=cast(Any, http),
        base_url=configured.slack_base_url,
        credential=EnvironmentOrFileCredential(value=None, path=credential_path),
        settings=configured,
    )

    assert asyncio.run(client.verify_identity(correlation_id="first")) is True
    assert asyncio.run(client.verify_identity(correlation_id="cached")) is True
    assert len(http.calls) == 1

    credential_path.write_text("token-b", encoding="utf-8")
    with pytest.raises(SlackRetryableFailure, match="Slack operation failed"):
        asyncio.run(
            client.post_notification(
                SlackMessageRequest(channel="C1234567890", text="bounded"),
                correlation_id="rotated-before-verification",
            )
        )
    assert len(http.calls) == 1

    assert asyncio.run(client.verify_identity(correlation_id="rotated")) is False
    with pytest.raises(SlackRetryableFailure, match="Slack operation failed"):
        asyncio.run(
            client.post_notification(
                SlackMessageRequest(channel="C1234567890", text="bounded"),
                correlation_id="rotated",
            )
        )
    assert len(http.calls) == 2
    assert all(call["operation"] is MetricOperation.IDENTITY for call in http.calls)


def test_slack_identity_cache_includes_expected_principal() -> None:
    http = _Http(
        [
            _response(_auth_payload()),
            _response(_auth_payload(team_id="T0000000000")),
        ]
    )
    client = _client(http)
    assert asyncio.run(client.verify_identity(correlation_id="first")) is True
    client._expected_team_id = "T0000000000"
    assert asyncio.run(client.verify_identity(correlation_id="changed-principal")) is True
    assert len(http.calls) == 2


def test_slack_identity_errors_never_expose_token_or_fingerprint(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "mounted-secret-never-expose"
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    credential_path = tmp_path / "slack-token"
    credential_path.write_text(token, encoding="utf-8")
    configured = _settings(slack_bot_token=None, slack_bot_token_file=credential_path)
    client = SlackClient(
        http=cast(Any, _Http([_response({"ok": False, "error": "invalid_auth"})])),
        base_url=configured.slack_base_url,
        credential=EnvironmentOrFileCredential(value=None, path=credential_path),
        settings=configured,
    )
    with pytest.raises(SlackPermanentFailure) as captured:
        asyncio.run(client.verify_identity(correlation_id="request"))
    rendered = f"{captured.value} {caplog.text}"
    assert token not in rendered
    assert fingerprint not in rendered


def test_missing_slack_credential_cannot_reuse_identity_cache() -> None:
    configured = _settings()
    client = SlackClient(
        http=cast(Any, _Http([])),
        base_url=configured.slack_base_url,
        credential=EnvironmentOrFileCredential(value=None, path=None),
        settings=configured,
    )
    with pytest.raises(SlackPermanentFailure) as captured:
        asyncio.run(client.verify_identity(correlation_id="request"))
    assert captured.value.error_code == "slack_credential_missing"
    assert str(captured.value) == "Slack operation failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("team_id", "T0000000000"),
        ("user_id", "U0000000000"),
        ("bot_id", "B0000000000"),
    ],
)
def test_slack_identity_mismatch_blocks_authorization(field: str, value: str) -> None:
    payload = {
        "ok": True,
        "team_id": "T1234567890",
        "user_id": "U1234567890",
        "bot_id": "B1234567890",
    }
    payload[field] = value
    assert (
        asyncio.run(_client(_Http([_response(payload)])).verify_identity(correlation_id="request"))
        is False
    )


def test_slack_identity_rejects_malformed_and_revoked_responses() -> None:
    with pytest.raises(SlackPermanentFailure):
        asyncio.run(
            _client(_Http([_response({"ok": True})])).verify_identity(correlation_id="request")
        )
    with pytest.raises(SlackPermanentFailure):
        asyncio.run(
            _client(_Http([_response({"ok": False, "error": "invalid_auth"})])).verify_identity(
                correlation_id="request"
            )
        )


def test_message_rendering_is_deterministic_bounded_and_accessible() -> None:
    first = render_slack_notification(
        decision=_decision(),
        canonical_service_id="payments-api",
        github_action=_github_action(),
        maximum_characters=1000,
    )
    second = render_slack_notification(
        decision=_decision(),
        canonical_service_id="payments-api",
        github_action=_github_action(),
        maximum_characters=1000,
    )
    assert first == second
    assert first.startswith("Deployment context sync\nDecision: ready")
    assert "payments-api" in first
    assert "#issuecomment-1000" in first
    assert "GitHub revision: 1" in first
    assert len(first.encode()) <= 4000
    with pytest.raises(ValueError):
        render_slack_notification(
            decision=_decision(),
            canonical_service_id="payments-api",
            github_action=_github_action(),
            maximum_characters=100,
        )


def test_slack_request_disables_unfurling_and_has_no_metadata() -> None:
    payload = SlackMessageRequest(
        channel="C1234567890",
        text="Deployment context sync",
    ).provider_payload()
    assert payload["mrkdwn"] is True
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False
    assert "metadata" not in payload


def test_slack_business_scope_varies_only_with_business_identity() -> None:
    base = slack_action_scope_key(
        deployment_scope_id="test-scope",
        expected_team_id="T1234567890",
        github_repository_id=424242,
        github_issue_number=42,
        canonical_service_id="payments-api",
        github_action_revision=1,
        channel_id="C1234567890",
    )
    assert base == slack_action_scope_key(
        deployment_scope_id="test-scope",
        expected_team_id="T1234567890",
        github_repository_id=424242,
        github_issue_number=42,
        canonical_service_id="payments-api",
        github_action_revision=1,
        channel_id="C1234567890",
    )
    assert base != slack_action_scope_key(
        deployment_scope_id="test-scope",
        expected_team_id="T1234567890",
        github_repository_id=424242,
        github_issue_number=42,
        canonical_service_id="payments-api",
        github_action_revision=2,
        channel_id="C1234567890",
    )
    assert base != slack_action_scope_key(
        deployment_scope_id="test-scope",
        expected_team_id="T1234567890",
        github_repository_id=424242,
        github_issue_number=42,
        canonical_service_id="payments-api",
        github_action_revision=1,
        channel_id="C0000000000",
    )
    other_team = slack_action_scope_key(
        deployment_scope_id="test-scope",
        expected_team_id="T0000000000",
        github_repository_id=424242,
        github_issue_number=42,
        canonical_service_id="payments-api",
        github_action_revision=1,
        channel_id="C1234567890",
    )
    assert base != other_team
    assert "slack_team:T1234567890" in base
    for execution_identifier in ("task", "attempt", "worker", "token"):
        assert execution_identifier not in base


def test_slack_snapshot_is_order_independent_and_evidence_sensitive() -> None:
    decision = _decision()
    reversed_decision = decision.model_copy(
        update={"source_references": tuple(reversed(decision.source_references))}
    )
    first = slack_decision_snapshot_hash(
        decision=decision,
        github_action=_github_action(),
        channel_id="C1234567890",
        expected_team_id="T1234567890",
    )
    second = slack_decision_snapshot_hash(
        decision=reversed_decision,
        github_action=_github_action(),
        channel_id="C1234567890",
        expected_team_id="T1234567890",
    )
    assert first == second
    assert first != slack_decision_snapshot_hash(
        decision=decision,
        github_action=_github_action(revision=2),
        channel_id="C1234567890",
        expected_team_id="T1234567890",
    )
    assert first != slack_decision_snapshot_hash(
        decision=decision,
        github_action=_github_action(),
        channel_id="C1234567890",
        expected_team_id="T0000000000",
    )


def test_confirmed_slack_success_requires_exact_channel_and_timestamp() -> None:
    http = _authorized_http(_response({"ok": True, "channel": "C1234567890", "ts": "1.000001"}))
    receipt = asyncio.run(
        _post_with_identity(
            _client(http),
            SlackMessageRequest(channel="C1234567890", text="bounded"),
        )
    )
    assert receipt.provider_resource_identifier == "C1234567890:1.000001"
    assert "metadata" not in cast(dict[str, object], http.calls[1]["json_body"])


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "channel": "C0000000000", "ts": "1.000001"},
        {"ok": True, "channel": "C1234567890", "ts": "invalid"},
        {"unexpected": "successful response"},
    ],
)
def test_malformed_successful_slack_response_is_outcome_unknown(
    payload: dict[str, object],
) -> None:
    with pytest.raises(SlackOutcomeUnknown):
        asyncio.run(
            _post_with_identity(
                _client(_authorized_http(_response(payload))),
                SlackMessageRequest(channel="C1234567890", text="bounded"),
            )
        )


def test_slack_rate_limit_is_known_retryable_and_bounded() -> None:
    with pytest.raises(SlackRetryableFailure) as captured:
        asyncio.run(
            _post_with_identity(
                _client(
                    _authorized_http(_response(None, status=429, headers={"retry-after": "999"}))
                ),
                SlackMessageRequest(channel="C1234567890", text="bounded"),
            )
        )
    assert captured.value.error_code == "slack_rate_limited"
    assert captured.value.retry_after_seconds == 60


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("channel_not_found", "slack_channel_not_found"),
        ("not_in_channel", "slack_bot_not_in_channel"),
        ("is_archived", "slack_channel_archived"),
        ("missing_scope", "slack_missing_scope"),
    ],
)
def test_slack_known_rejections_are_safely_classified(error: str, code: str) -> None:
    with pytest.raises(SlackPermanentFailure) as captured:
        asyncio.run(
            _post_with_identity(
                _client(_authorized_http(_response({"ok": False, "error": error}))),
                SlackMessageRequest(channel="C1234567890", text="bounded"),
            )
        )
    assert captured.value.error_code == code
    assert error not in str(captured.value)


def test_metrics_reject_high_cardinality_or_untyped_labels() -> None:
    metrics = InMemoryMetricsSink()
    with pytest.raises(ValueError):
        metrics.increment(
            MetricName.DUPLICATE_ACTION_PREVENTION_COUNT,
            labels=cast(
                Any,
                {
                    MetricLabel.PROVIDER: MetricProvider.SLACK,
                    MetricLabel.OPERATION: MetricOperation.SLACK_NOTIFICATION,
                    "task_id": "high-cardinality",
                },
            ),
        )
    with pytest.raises(ValueError):
        metrics.increment(
            MetricName.DUPLICATE_ACTION_PREVENTION_COUNT,
            labels=cast(
                Any,
                {
                    MetricLabel.PROVIDER: "slack",
                    MetricLabel.OPERATION: "post_deployment_context_notification",
                },
            ),
        )


def test_slack_result_contract_requires_provider_evidence_for_success() -> None:
    from ninjatech_deployment_lab.integrations.domain import SlackNotificationResult

    with pytest.raises(ValidationError):
        SlackNotificationResult(
            requested=True,
            state=SlackDeliveryState.SUCCEEDED,
        )


@dataclass
class _Notifier:
    outcome: SlackMessageReceipt | Exception
    customer_cancellation: asyncio.Event | None = None
    ownership_lost: asyncio.Event | None = None
    identity: bool | Exception = True
    identity_customer_cancellation: asyncio.Event | None = None
    identity_ownership_lost: asyncio.Event | None = None
    identity_calls: int = 0
    post_calls: int = 0

    async def verify_identity(self, *, correlation_id: str) -> bool:
        self.identity_calls += 1
        if self.identity_customer_cancellation is not None:
            self.identity_customer_cancellation.set()
        if self.identity_ownership_lost is not None:
            self.identity_ownership_lost.set()
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    async def post_notification(
        self,
        request: SlackMessageRequest,
        *,
        correlation_id: str,
    ) -> SlackMessageReceipt:
        self.post_calls += 1
        if self.customer_cancellation is not None:
            self.customer_cancellation.set()
        if self.ownership_lost is not None:
            self.ownership_lost.set()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Actions:
    def __init__(self, *, status: ExternalActionStatus = ExternalActionStatus.RESERVED) -> None:
        now = datetime.now(UTC)
        self.action = ExternalAction(
            id=uuid4(),
            task_id=uuid4(),
            current_attempt_id=uuid4(),
            provider="slack",
            operation="post_deployment_context_notification",
            action_scope_key="scope",
            desired_request_fingerprint="a" * 64,
            decision_snapshot_hash="b" * 64,
            revision=1,
            status=status.value,
            action_attempt_count=1,
            reserved_at=now,
            last_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        if status is ExternalActionStatus.SUCCEEDED:
            self.action.provider_resource_identifier = "C1234567890:1.000001"
            self.action.applied_request_fingerprint = "a" * 64
            self.action.completed_at = now
        self.reserve_kind = ActionReservationKind.REPLAY
        self.transitions: list[ExternalActionStatus] = []
        self.transition_calls: list[dict[str, Any]] = []
        self.delay_seconds = 0.0

    async def reserve_or_get(self, **kwargs: Any) -> ActionReservation:
        self.action.action_scope_key = cast(str, kwargs["action_scope_key"])
        self.action.desired_request_fingerprint = cast(str, kwargs["desired_request_fingerprint"])
        self.action.decision_snapshot_hash = cast(str, kwargs["decision_snapshot_hash"])
        return ActionReservation(self.action, self.reserve_kind)

    async def transition(
        self,
        *,
        new_status: ExternalActionStatus,
        values: dict[str, Any] | None = None,
        error_code: str | None = None,
        **kwargs: Any,
    ) -> ExternalAction:
        self.transitions.append(new_status)
        self.transition_calls.append(
            {
                "new_status": new_status,
                "values": values,
                "error_code": error_code,
                **kwargs,
            }
        )
        self.action.status = new_status.value
        self.action.last_error_code = error_code
        for key, value in (values or {}).items():
            setattr(self.action, key, value)
        if new_status is ExternalActionStatus.SUCCEEDED:
            self.action.completed_at = datetime.now(UTC)
        return self.action

    async def reconciliation_delay_seconds(self, action_id: object) -> float:
        return self.delay_seconds


def _execution_context(
    *,
    customer_cancellation: asyncio.Event | None = None,
    ownership_lost: asyncio.Event | None = None,
) -> tuple[TaskExecution, HandlerContext]:
    task_id = uuid4()
    attempt_id = uuid4()
    fence = ExecutionFence(
        task_id=task_id,
        attempt_id=attempt_id,
        attempt_number=1,
        worker_id="worker-1",
        lease_token_hash="f" * 64,
    )
    task = TaskExecution(
        task_id=task_id,
        task_type="deployment_context_sync",
        task_input={},
        attempt_id=attempt_id,
        attempt_number=1,
        max_attempts=3,
        execution_fence=fence,
    )
    context = HandlerContext(
        task_id=task_id,
        attempt_id=attempt_id,
        attempt_number=1,
        worker_id="worker-1",
        customer_cancellation=customer_cancellation or asyncio.Event(),
        ownership_lost=ownership_lost or asyncio.Event(),
    )
    return task, context


def _delivery_request() -> SlackDeliveryRequest:
    return SlackDeliveryRequest(
        channel_id="C1234567890",
        canonical_service_id="payments-api",
        github_repository_id=424242,
        github_issue_number=42,
        github_action=_github_action(),
        decision=_decision(),
    )


def _delivery(
    notifier: _Notifier,
    actions: _Actions,
) -> SlackDeliveryService:
    return SlackDeliveryService(
        settings=_settings(),
        notifier=notifier,
        actions=cast(Any, actions),
        metrics=InMemoryMetricsSink(),
    )


def test_succeeded_slack_action_replays_without_posting() -> None:
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="2.000001"),
        identity=SlackRetryableFailure("slack_identity_unavailable"),
    )
    actions = _Actions(status=ExternalActionStatus.SUCCEEDED)
    task, context = _execution_context()
    result = asyncio.run(
        _delivery(notifier, actions).deliver(
            task=task,
            context=context,
            request=_delivery_request(),
        )
    )
    assert result.state is SlackDeliveryState.SUCCEEDED
    assert result.action is not None
    assert result.action.provider_resource_identifier == "C1234567890:1.000001"
    assert notifier.identity_calls == 0
    assert notifier.post_calls == 0


@pytest.mark.parametrize(
    ("error", "state", "persisted"),
    [
        (
            SlackRetryableFailure("slack_write_pretransmission_failure"),
            SlackDeliveryState.RETRYABLE_FAILURE,
            ExternalActionStatus.RETRYABLE_FAILURE,
        ),
        (
            SlackPermanentFailure("slack_channel_not_found"),
            SlackDeliveryState.PERMANENT_FAILURE,
            ExternalActionStatus.PERMANENT_FAILURE,
        ),
        (
            SlackOutcomeUnknown("slack_write_outcome_unknown"),
            SlackDeliveryState.OUTCOME_UNKNOWN,
            ExternalActionStatus.OUTCOME_UNKNOWN,
        ),
    ],
)
def test_slack_failures_are_degraded_and_truthfully_persisted(
    error: Exception,
    state: SlackDeliveryState,
    persisted: ExternalActionStatus,
) -> None:
    notifier = _Notifier(error)
    actions = _Actions()
    task, context = _execution_context()
    result = asyncio.run(
        _delivery(notifier, actions).deliver(
            task=task,
            context=context,
            request=_delivery_request(),
        )
    )
    assert result.state is state
    assert ExternalActionStatus(actions.action.status) is persisted
    assert notifier.post_calls == 1


def test_unknown_slack_outcome_is_never_blindly_reposted() -> None:
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="2.000001"),
        identity=SlackRetryableFailure("slack_identity_unavailable"),
    )
    actions = _Actions(status=ExternalActionStatus.OUTCOME_UNKNOWN)
    actions.action.last_error_code = "slack_write_outcome_unknown"
    task, context = _execution_context()
    result = asyncio.run(
        _delivery(notifier, actions).deliver(
            task=task,
            context=context,
            request=_delivery_request(),
        )
    )
    assert result.state is SlackDeliveryState.OUTCOME_UNKNOWN
    assert notifier.identity_calls == 0
    assert notifier.post_calls == 0


def test_persisted_permanent_failure_replays_without_slack_access() -> None:
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="2.000001"),
        identity=SlackRetryableFailure("slack_identity_unavailable"),
    )
    actions = _Actions(status=ExternalActionStatus.PERMANENT_FAILURE)
    actions.action.last_error_code = "slack_channel_not_found"
    task, context = _execution_context()
    result = asyncio.run(
        _delivery(notifier, actions).deliver(
            task=task,
            context=context,
            request=_delivery_request(),
        )
    )
    assert result.state is SlackDeliveryState.PERMANENT_FAILURE
    assert result.safe_error_code == "slack_channel_not_found"
    assert notifier.identity_calls == 0
    assert notifier.post_calls == 0


def test_retryable_action_waits_for_database_not_before_without_slack_access() -> None:
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="2.000001"),
        identity=SlackRetryableFailure("slack_identity_unavailable"),
    )
    actions = _Actions(status=ExternalActionStatus.RETRYABLE_FAILURE)
    actions.action.last_error_code = "slack_rate_limited"
    actions.delay_seconds = 30.0
    task, context = _execution_context()
    result = asyncio.run(
        _delivery(notifier, actions).deliver(
            task=task,
            context=context,
            request=_delivery_request(),
        )
    )
    assert result.state is SlackDeliveryState.RETRYABLE_FAILURE
    assert result.safe_error_code == "slack_rate_limited"
    assert notifier.identity_calls == 0
    assert notifier.post_calls == 0


@pytest.mark.parametrize(
    "identity",
    [
        True,
        False,
        SlackRetryableFailure("slack_identity_unavailable"),
        SlackPermanentFailure("slack_auth_rejected"),
    ],
)
def test_customer_cancellation_during_identity_never_degrades_or_posts(
    identity: bool | Exception,
) -> None:
    cancellation = asyncio.Event()
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="1.000001"),
        identity=identity,
        identity_customer_cancellation=cancellation,
    )
    actions = _Actions()
    task, context = _execution_context(customer_cancellation=cancellation)
    with pytest.raises(TaskCancelled):
        asyncio.run(
            _delivery(notifier, actions).deliver(
                task=task,
                context=context,
                request=_delivery_request(),
            )
        )
    assert actions.transitions == []
    assert notifier.identity_calls == 1
    assert notifier.post_calls == 0


def test_ownership_loss_during_identity_blocks_action_mutation_and_post() -> None:
    ownership = asyncio.Event()
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="1.000001"),
        identity_ownership_lost=ownership,
    )
    actions = _Actions()
    task, context = _execution_context(ownership_lost=ownership)
    with pytest.raises(OwnershipLostError):
        asyncio.run(
            _delivery(notifier, actions).deliver(
                task=task,
                context=context,
                request=_delivery_request(),
            )
        )
    assert actions.transitions == []
    assert notifier.identity_calls == 1
    assert notifier.post_calls == 0


@pytest.mark.parametrize(
    ("identity", "expected_state", "expected_status"),
    [
        (
            SlackRetryableFailure("slack_identity_unavailable"),
            SlackDeliveryState.RETRYABLE_FAILURE,
            ExternalActionStatus.RETRYABLE_FAILURE,
        ),
        (
            SlackPermanentFailure("slack_auth_rejected"),
            SlackDeliveryState.PERMANENT_FAILURE,
            ExternalActionStatus.PERMANENT_FAILURE,
        ),
        (
            False,
            SlackDeliveryState.NEEDS_HUMAN_REVIEW,
            ExternalActionStatus.NEEDS_HUMAN_REVIEW,
        ),
    ],
)
def test_identity_failure_without_cancellation_is_persisted_degradation(
    identity: bool | Exception,
    expected_state: SlackDeliveryState,
    expected_status: ExternalActionStatus,
) -> None:
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="1.000001"),
        identity=identity,
    )
    actions = _Actions()
    task, context = _execution_context()
    github_action = _delivery_request().github_action
    result = asyncio.run(
        _delivery(notifier, actions).deliver(
            task=task,
            context=context,
            request=_delivery_request(),
        )
    )
    assert result.state is expected_state
    assert ExternalActionStatus(actions.action.status) is expected_status
    assert github_action.provider_resource_identifier == "1000"
    assert notifier.post_calls == 0


def test_customer_cancellation_before_slack_write_posts_nothing() -> None:
    cancellation = asyncio.Event()
    cancellation.set()
    notifier = _Notifier(SlackMessageReceipt(channel="C1234567890", timestamp="1.000001"))
    actions = _Actions()
    task, context = _execution_context(customer_cancellation=cancellation)
    with pytest.raises(TaskCancelled):
        asyncio.run(
            _delivery(notifier, actions).deliver(
                task=task,
                context=context,
                request=_delivery_request(),
            )
        )
    assert notifier.identity_calls == 0
    assert notifier.post_calls == 0
    assert actions.transitions == []


def test_customer_cancellation_after_slack_success_preserves_provider_truth() -> None:
    cancellation = asyncio.Event()
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="1.000001"),
        customer_cancellation=cancellation,
    )
    actions = _Actions()
    task, context = _execution_context(customer_cancellation=cancellation)
    with pytest.raises(TaskCancelled):
        asyncio.run(
            _delivery(notifier, actions).deliver(
                task=task,
                context=context,
                request=_delivery_request(),
            )
        )
    assert ExternalActionStatus(actions.action.status) is ExternalActionStatus.SUCCEEDED
    assert actions.action.provider_resource_identifier == "C1234567890:1.000001"
    assert notifier.post_calls == 1


def test_ownership_loss_after_slack_acceptance_blocks_stale_finalization() -> None:
    ownership = asyncio.Event()
    notifier = _Notifier(
        SlackMessageReceipt(channel="C1234567890", timestamp="1.000001"),
        ownership_lost=ownership,
    )
    actions = _Actions()
    task, context = _execution_context(ownership_lost=ownership)
    with pytest.raises(OwnershipLostError):
        asyncio.run(
            _delivery(notifier, actions).deliver(
                task=task,
                context=context,
                request=_delivery_request(),
            )
        )
    assert ExternalActionStatus(actions.action.status) is ExternalActionStatus.EXECUTING
    assert actions.action.provider_resource_identifier is None

    replacement = _Notifier(SlackMessageReceipt(channel="C1234567890", timestamp="2.000001"))
    replacement_task, replacement_context = _execution_context()
    result = asyncio.run(
        _delivery(replacement, actions).deliver(
            task=replacement_task,
            context=replacement_context,
            request=_delivery_request(),
        )
    )
    assert result.state is SlackDeliveryState.OUTCOME_UNKNOWN
    assert replacement.identity_calls == 0
    assert replacement.post_calls == 0


def test_slack_delivery_logs_exclude_token_message_and_response_payload() -> None:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    metrics_logger = logging.getLogger("ninjatech_deployment_lab.integrations.metrics")
    capture = _Capture()
    original_propagate = metrics_logger.propagate
    original_level = metrics_logger.level
    metrics_logger.addHandler(capture)
    metrics_logger.propagate = False
    metrics_logger.setLevel(logging.INFO)
    notifier = _Notifier(SlackMessageReceipt(channel="C1234567890", timestamp="1.000001"))
    actions = _Actions()
    task, context = _execution_context()
    delivery = SlackDeliveryService(
        settings=_settings(slack_bot_token=SecretStr("slack-token-never-log")),
        notifier=notifier,
        actions=cast(Any, actions),
        metrics=StructuredLoggingMetricsSink(),
    )
    try:
        result = asyncio.run(
            delivery.deliver(
                task=task,
                context=context,
                request=_delivery_request(),
            )
        )
    finally:
        metrics_logger.removeHandler(capture)
        metrics_logger.propagate = original_propagate
        metrics_logger.setLevel(original_level)
    assert result.state is SlackDeliveryState.SUCCEEDED
    assert any(
        getattr(record, "metric_name", None) == "slack_delivery_state_count" for record in records
    )
    rendered = " ".join(f"{record.getMessage()} {record.__dict__!r}" for record in records)
    assert "slack-token-never-log" not in rendered
    assert "Deployment context sync" not in rendered
    assert "1.000001" not in rendered
