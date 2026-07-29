from __future__ import annotations

import hashlib
import html
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import Field

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.credentials import CredentialProvider
from ninjatech_deployment_lab.integrations.domain import (
    WORKFLOW_VERSION,
    DeploymentContextDecision,
    ExternalActionReference,
    SlackDeliveryState,
    SlackNotificationResult,
    StrictModel,
    sha256_json,
)
from ninjatech_deployment_lab.integrations.http import (
    AmbiguousWriteError,
    IntegrationHttpClient,
    JsonHttpResponse,
    ProviderContractError,
    RetryableHttpError,
)
from ninjatech_deployment_lab.integrations.metrics import (
    MetricLabel,
    MetricName,
    MetricOperation,
    MetricProvider,
    MetricsSink,
)
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionOperation,
    ExternalActionProvider,
    ExternalActionStatus,
)
from ninjatech_deployment_lab.integrations.persistence import (
    ActionReservationKind,
    ExternalActionRepository,
)
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import ExecutionInvariantError
from ninjatech_deployment_lab.worker.handlers import HandlerContext, TaskExecution

_MESSAGE_TIMESTAMP = re.compile(r"^[0-9]{1,20}\.[0-9]{1,12}$")
_IDENTITY_CACHE_SECONDS = 300.0
_MAX_MESSAGE_BYTES = 4000
_MAX_MESSAGE_LINES = 6
_MAX_URL_CHARS = 2000
_DEFAULT_RETRY_DELAY_SECONDS = 5.0


class SlackError(Exception):
    def __init__(self, error_code: str, *, retry_after_seconds: float | None = None) -> None:
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Slack operation failed")


class SlackRetryableFailure(SlackError):
    """The request is known not to have produced a Slack message."""


class SlackPermanentFailure(SlackError):
    """Slack definitively rejected the request or credential."""


class SlackOutcomeUnknown(SlackError):
    """Slack may have accepted a message; automatic resend is forbidden."""


class SlackIdentity(StrictModel):
    team_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    bot_id: str | None = Field(default=None, min_length=1, max_length=255)


@dataclass(frozen=True, slots=True)
class _SlackIdentityCache:
    credential_fingerprint: bytes
    expected_team_id: str | None
    expected_user_id: str | None
    expected_bot_id: str | None
    verified_until: float


class SlackMessageRequest(StrictModel):
    channel: str = Field(pattern=r"^[CG][A-Z0-9]{8,30}$")
    text: str = Field(min_length=1, max_length=4000)
    mrkdwn: bool = True
    unfurl_links: bool = False
    unfurl_media: bool = False

    def provider_payload(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


class SlackMessageReceipt(StrictModel):
    channel: str = Field(pattern=r"^[CG][A-Z0-9]{8,30}$")
    timestamp: str = Field(pattern=r"^[0-9]{1,20}\.[0-9]{1,12}$")

    @property
    def provider_resource_identifier(self) -> str:
        return f"{self.channel}:{self.timestamp}"


class SlackNotifier(Protocol):
    async def verify_identity(self, *, correlation_id: str) -> bool: ...

    async def post_notification(
        self,
        request: SlackMessageRequest,
        *,
        correlation_id: str,
    ) -> SlackMessageReceipt: ...


class SlackClient:
    """Focused Slack REST connector with exact configured-ID authorization."""

    def __init__(
        self,
        *,
        http: IntegrationHttpClient,
        base_url: str,
        credential: CredentialProvider,
        settings: Settings,
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._credential = credential
        self._expected_team_id = settings.slack_expected_team_id
        self._expected_user_id = settings.slack_expected_user_id
        self._expected_bot_id = settings.slack_expected_bot_id
        self._write_timeout = settings.slack_write_timeout_seconds
        self._maximum_retry_after = settings.integration_max_retry_after_seconds
        self._identity_cache: _SlackIdentityCache | None = None

    async def verify_identity(self, *, correlation_id: str) -> bool:
        token, credential_fingerprint = self._current_credential()
        if self._cache_authorizes(credential_fingerprint):
            return True
        try:
            response = await self._request(
                path="auth.test",
                payload={},
                correlation_id=correlation_id,
                write=False,
                operation=MetricOperation.IDENTITY,
                token=token,
            )
        except RetryableHttpError:
            raise SlackRetryableFailure("slack_identity_unavailable") from None
        except ProviderContractError:
            raise SlackPermanentFailure("slack_auth_malformed") from None
        self._classify_read_response(response.status_code, response.headers)
        payload = _object(response.payload, error=SlackPermanentFailure("slack_auth_malformed"))
        if payload.get("ok") is not True:
            raise _slack_api_error(payload, identity=True)
        identity = _identity(payload)
        # Slack IDs are opaque and compared exactly; display names never authorize.
        valid = (
            identity.team_id == self._expected_team_id
            and identity.user_id == self._expected_user_id
            and (self._expected_bot_id is None or identity.bot_id == self._expected_bot_id)
        )
        if valid:
            self._identity_cache = _SlackIdentityCache(
                credential_fingerprint=credential_fingerprint,
                expected_team_id=self._expected_team_id,
                expected_user_id=self._expected_user_id,
                expected_bot_id=self._expected_bot_id,
                verified_until=time.monotonic() + _IDENTITY_CACHE_SECONDS,
            )
        else:
            self._identity_cache = None
        return valid

    async def post_notification(
        self,
        request: SlackMessageRequest,
        *,
        correlation_id: str,
    ) -> SlackMessageReceipt:
        token, credential_fingerprint = self._current_credential()
        if not self._cache_authorizes(credential_fingerprint):
            raise SlackRetryableFailure("slack_identity_verification_required")
        try:
            response = await self._request(
                path="chat.postMessage",
                payload=request.provider_payload(),
                correlation_id=correlation_id,
                write=True,
                operation=MetricOperation.WRITE,
                token=token,
            )
        except AmbiguousWriteError:
            raise SlackOutcomeUnknown("slack_write_outcome_unknown") from None
        except RetryableHttpError:
            raise SlackRetryableFailure("slack_write_pretransmission_failure") from None
        except ProviderContractError:
            raise SlackOutcomeUnknown("slack_write_outcome_unknown") from None

        if response.status_code == 429:
            raise SlackRetryableFailure(
                "slack_rate_limited",
                retry_after_seconds=_retry_after(
                    response.headers,
                    maximum=self._maximum_retry_after,
                ),
            )
        if response.status_code in {408, 500, 502, 503, 504}:
            raise SlackOutcomeUnknown("slack_write_outcome_unknown")
        if response.status_code in {401, 403}:
            raise SlackPermanentFailure("slack_auth_rejected")
        if not 200 <= response.status_code < 300:
            raise SlackPermanentFailure("slack_write_rejected")

        payload = _object(
            response.payload,
            error=SlackOutcomeUnknown("slack_write_outcome_unknown"),
        )
        if not isinstance(payload.get("ok"), bool):
            raise SlackOutcomeUnknown("slack_write_outcome_unknown")
        if payload["ok"] is not True:
            raise _slack_api_error(payload, identity=False)
        channel = payload.get("channel")
        timestamp = payload.get("ts")
        if (
            not isinstance(channel, str)
            or channel != request.channel
            or not isinstance(timestamp, str)
            or _MESSAGE_TIMESTAMP.fullmatch(timestamp) is None
        ):
            raise SlackOutcomeUnknown("slack_write_outcome_unknown")
        return SlackMessageReceipt(channel=channel, timestamp=timestamp)

    async def _request(
        self,
        *,
        path: str,
        payload: dict[str, JsonValue],
        correlation_id: str,
        write: bool,
        operation: MetricOperation,
        token: str,
    ) -> JsonHttpResponse:
        return await self._http.request_json(
            method="POST",
            base_url=self._base_url,
            path=path,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json_body=payload,
            correlation_id=correlation_id,
            write=write,
            timeout_seconds=self._write_timeout if write else None,
            provider=MetricProvider.SLACK,
            operation=operation,
        )

    def _current_credential(self) -> tuple[str, bytes]:
        try:
            token = self._credential.get_secret()
        except (OSError, ValueError):
            raise SlackPermanentFailure("slack_credential_missing") from None
        if token is None or not token:
            raise SlackPermanentFailure("slack_credential_missing")
        return token, hashlib.sha256(token.encode()).digest()

    def _cache_authorizes(self, credential_fingerprint: bytes) -> bool:
        cache = self._identity_cache
        return (
            cache is not None
            and time.monotonic() < cache.verified_until
            and credential_fingerprint == cache.credential_fingerprint
            and self._expected_team_id == cache.expected_team_id
            and self._expected_user_id == cache.expected_user_id
            and self._expected_bot_id == cache.expected_bot_id
        )

    def _classify_read_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        if 200 <= status_code < 300:
            return
        if status_code == 429:
            raise SlackRetryableFailure(
                "slack_rate_limited",
                retry_after_seconds=_retry_after(
                    headers,
                    maximum=self._maximum_retry_after,
                ),
            )
        if status_code in {408, 500, 502, 503, 504}:
            raise SlackRetryableFailure("slack_identity_unavailable")
        if status_code in {401, 403}:
            raise SlackPermanentFailure("slack_auth_rejected")
        raise SlackPermanentFailure("slack_identity_rejected")


@dataclass(frozen=True, slots=True)
class SlackDeliveryRequest:
    channel_id: str
    canonical_service_id: str
    github_repository_id: int
    github_issue_number: int
    github_action: ExternalActionReference
    decision: DeploymentContextDecision


class SlackDeliveryService:
    """Secondary delivery that never changes the authoritative GitHub outcome."""

    def __init__(
        self,
        *,
        settings: Settings,
        notifier: SlackNotifier,
        actions: ExternalActionRepository,
        metrics: MetricsSink,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        self._actions = actions
        self._metrics = metrics

    async def deliver(
        self,
        *,
        task: TaskExecution,
        context: HandlerContext,
        request: SlackDeliveryRequest,
    ) -> SlackNotificationResult:
        context.raise_if_cancelled()
        message = render_slack_notification(
            decision=request.decision,
            canonical_service_id=request.canonical_service_id,
            github_action=request.github_action,
            maximum_characters=self._settings.slack_max_text_chars,
        )
        provider_request = SlackMessageRequest(
            channel=request.channel_id,
            text=message,
        )
        scope = slack_action_scope_key(
            deployment_scope_id=self._settings.deployment_scope_id,
            expected_team_id=self._settings.slack_expected_team_id,
            github_repository_id=request.github_repository_id,
            github_issue_number=request.github_issue_number,
            canonical_service_id=request.canonical_service_id,
            github_action_revision=request.github_action.revision,
            channel_id=request.channel_id,
        )
        desired_fingerprint = sha256_json(provider_request.provider_payload())
        snapshot_hash = slack_decision_snapshot_hash(
            decision=request.decision,
            github_action=request.github_action,
            channel_id=request.channel_id,
            expected_team_id=self._settings.slack_expected_team_id,
        )
        reservation = await self._actions.reserve_or_get(
            fence=task.execution_fence,
            provider=ExternalActionProvider.SLACK,
            operation=ExternalActionOperation.POST_DEPLOYMENT_CONTEXT_NOTIFICATION,
            action_scope_key=scope,
            desired_request_fingerprint=desired_fingerprint,
            decision_snapshot_hash=snapshot_hash,
        )
        action = reservation.action

        if reservation.kind in {
            ActionReservationKind.CHANGED,
            ActionReservationKind.SOURCE_DRIFT,
        }:
            return self._result(
                SlackDeliveryState.NEEDS_HUMAN_REVIEW,
                action=action,
                error_code="slack_action_evidence_changed",
            )
        status = ExternalActionStatus(action.status)
        if status is ExternalActionStatus.SUCCEEDED:
            return self._result(SlackDeliveryState.SUCCEEDED, action=action)
        if status is ExternalActionStatus.OUTCOME_UNKNOWN:
            return self._result(
                SlackDeliveryState.OUTCOME_UNKNOWN,
                action=action,
                error_code=action.last_error_code or "slack_manual_reconciliation_required",
            )
        if status in {
            ExternalActionStatus.PERMANENT_FAILURE,
            ExternalActionStatus.NEEDS_HUMAN_REVIEW,
        }:
            state = (
                SlackDeliveryState.PERMANENT_FAILURE
                if status is ExternalActionStatus.PERMANENT_FAILURE
                else SlackDeliveryState.NEEDS_HUMAN_REVIEW
            )
            return self._result(
                state,
                action=action,
                error_code=action.last_error_code or "slack_delivery_requires_attention",
            )
        if status is ExternalActionStatus.EXECUTING:
            context.raise_if_cancelled()
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.EXECUTING},
                new_status=ExternalActionStatus.OUTCOME_UNKNOWN,
                transition="slack_previous_write_unresolved",
                error_code="slack_manual_reconciliation_required",
            )
            return self._result(
                SlackDeliveryState.OUTCOME_UNKNOWN,
                action=action,
                error_code="slack_manual_reconciliation_required",
            )

        if status is ExternalActionStatus.RETRYABLE_FAILURE:
            delay_seconds = await self._actions.reconciliation_delay_seconds(action.id)
            context.raise_if_cancelled()
            if delay_seconds > 0:
                return self._result(
                    SlackDeliveryState.RETRYABLE_FAILURE,
                    action=action,
                    error_code=action.last_error_code or "slack_retry_not_ready",
                )

        context.raise_if_cancelled()
        try:
            identity_verified = await self._notifier.verify_identity(
                correlation_id=str(task.task_id)
            )
        except SlackRetryableFailure as error:
            context.raise_if_cancelled()
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={
                    ExternalActionStatus.RESERVED,
                    ExternalActionStatus.READY_TO_EXECUTE,
                    ExternalActionStatus.RETRYABLE_FAILURE,
                },
                new_status=ExternalActionStatus.RETRYABLE_FAILURE,
                transition="slack_identity_retryable_failure",
                values={"write_started_at": None},
                error_code=error.error_code,
                not_before_delay_seconds=_known_unsent_delay(error),
            )
            context.raise_if_cancelled()
            return self._result(
                SlackDeliveryState.RETRYABLE_FAILURE,
                action=action,
                error_code=error.error_code,
            )
        except SlackPermanentFailure as error:
            context.raise_if_cancelled()
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={
                    ExternalActionStatus.RESERVED,
                    ExternalActionStatus.READY_TO_EXECUTE,
                    ExternalActionStatus.RETRYABLE_FAILURE,
                },
                new_status=ExternalActionStatus.PERMANENT_FAILURE,
                transition="slack_identity_permanent_failure",
                values={
                    "write_started_at": None,
                    "reconcile_not_before": None,
                },
                error_code=error.error_code,
            )
            context.raise_if_cancelled()
            return self._result(
                SlackDeliveryState.PERMANENT_FAILURE,
                action=action,
                error_code=error.error_code,
            )
        context.raise_if_cancelled()
        if not identity_verified:
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={
                    ExternalActionStatus.RESERVED,
                    ExternalActionStatus.READY_TO_EXECUTE,
                    ExternalActionStatus.RETRYABLE_FAILURE,
                },
                new_status=ExternalActionStatus.NEEDS_HUMAN_REVIEW,
                transition="slack_identity_mismatch",
                values={
                    "write_started_at": None,
                    "reconcile_not_before": None,
                },
                error_code="slack_identity_mismatch",
            )
            context.raise_if_cancelled()
            return self._result(
                SlackDeliveryState.NEEDS_HUMAN_REVIEW,
                action=action,
                error_code="slack_identity_mismatch",
            )

        context.raise_if_cancelled()
        action = await self._actions.transition(
            fence=task.execution_fence,
            action_id=action.id,
            expected_statuses={
                ExternalActionStatus.RESERVED,
                ExternalActionStatus.READY_TO_EXECUTE,
                ExternalActionStatus.RETRYABLE_FAILURE,
            },
            new_status=ExternalActionStatus.EXECUTING,
            transition="slack_write_started",
            settlement_delay_seconds=self._settings.slack_write_timeout_seconds,
        )
        context.raise_if_cancelled()
        try:
            receipt = await self._notifier.post_notification(
                provider_request,
                correlation_id=str(task.task_id),
            )
        except SlackRetryableFailure as error:
            context.raise_if_ownership_lost()
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.EXECUTING},
                new_status=ExternalActionStatus.RETRYABLE_FAILURE,
                transition="slack_retryable_failure",
                values={"write_started_at": None},
                error_code=error.error_code,
                not_before_delay_seconds=_known_unsent_delay(error),
            )
            context.raise_if_cancelled()
            return self._result(
                SlackDeliveryState.RETRYABLE_FAILURE,
                action=action,
                error_code=error.error_code,
            )
        except SlackPermanentFailure as error:
            context.raise_if_ownership_lost()
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.EXECUTING},
                new_status=ExternalActionStatus.PERMANENT_FAILURE,
                transition="slack_permanent_failure",
                error_code=error.error_code,
            )
            context.raise_if_cancelled()
            return self._result(
                SlackDeliveryState.PERMANENT_FAILURE,
                action=action,
                error_code=error.error_code,
            )
        except SlackOutcomeUnknown as error:
            context.raise_if_ownership_lost()
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.EXECUTING},
                new_status=ExternalActionStatus.OUTCOME_UNKNOWN,
                transition="slack_outcome_unknown",
                error_code=error.error_code,
            )
            context.raise_if_cancelled()
            return self._result(
                SlackDeliveryState.OUTCOME_UNKNOWN,
                action=action,
                error_code=error.error_code,
            )

        context.raise_if_ownership_lost()
        action = await self._actions.transition(
            fence=task.execution_fence,
            action_id=action.id,
            expected_statuses={ExternalActionStatus.EXECUTING},
            new_status=ExternalActionStatus.SUCCEEDED,
            transition="slack_write_succeeded",
            values={
                "provider_resource_identifier": receipt.provider_resource_identifier,
                "provider_url": None,
                "response_fingerprint": sha256_json(
                    {"channel": receipt.channel, "timestamp": receipt.timestamp}
                ),
                "applied_request_fingerprint": desired_fingerprint,
            },
        )
        context.raise_if_cancelled()
        return self._result(SlackDeliveryState.SUCCEEDED, action=action)

    def _result(
        self,
        state: SlackDeliveryState,
        *,
        action: ExternalAction | None,
        error_code: str | None = None,
    ) -> SlackNotificationResult:
        self._metrics.increment(
            MetricName.SLACK_DELIVERY_STATE_COUNT,
            labels={MetricLabel.DELIVERY_STATE: state},
        )
        if state is SlackDeliveryState.OUTCOME_UNKNOWN:
            self._metrics.increment(
                MetricName.OUTCOME_UNKNOWN_COUNT,
                labels={
                    MetricLabel.PROVIDER: MetricProvider.SLACK,
                    MetricLabel.OPERATION: MetricOperation.SLACK_NOTIFICATION,
                },
            )
        return SlackNotificationResult(
            requested=True,
            state=state,
            action=_action_reference(action) if action is not None else None,
            safe_error_code=error_code,
        )


def render_slack_notification(
    *,
    decision: DeploymentContextDecision,
    canonical_service_id: str,
    github_action: ExternalActionReference,
    maximum_characters: int,
) -> str:
    url = github_action.provider_url
    if url is None or len(url) > _MAX_URL_CHARS:
        raise ValueError("confirmed GitHub action requires a bounded URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("GitHub action URL must be absolute HTTP(S)")
    service = html.escape(canonical_service_id, quote=False)
    safe_url = html.escape(url, quote=True)
    text = (
        "Deployment context sync\n"
        f"Decision: {decision.outcome.value}\n"
        f"Service: {service}\n"
        f"Authoritative GitHub: <{safe_url}|deployment-context comment>\n"
        f"GitHub revision: {github_action.revision}"
    )
    if (
        len(text) > maximum_characters
        or len(text.encode()) > _MAX_MESSAGE_BYTES
        or len(text.splitlines()) > _MAX_MESSAGE_LINES
    ):
        raise ValueError("Slack notification exceeds its bounded message contract")
    return text


def slack_action_scope_key(
    *,
    deployment_scope_id: str | None,
    expected_team_id: str | None,
    github_repository_id: int,
    github_issue_number: int,
    canonical_service_id: str,
    github_action_revision: int,
    channel_id: str,
) -> str:
    if deployment_scope_id is None:
        raise ValueError("deployment scope is required")
    if expected_team_id is None:
        raise ValueError("expected Slack team is required")
    return (
        f"{WORKFLOW_VERSION}:{deployment_scope_id}:slack_team:{expected_team_id}:"
        f"{github_repository_id}:"
        f"{github_issue_number}:{canonical_service_id}:"
        f"github_revision:{github_action_revision}:"
        f"slack_channel:{channel_id}:notification"
    )


def slack_decision_snapshot_hash(
    *,
    decision: DeploymentContextDecision,
    github_action: ExternalActionReference,
    channel_id: str,
    expected_team_id: str | None,
) -> str:
    if expected_team_id is None:
        raise ValueError("expected Slack team is required")
    references = sorted(
        decision.source_references,
        key=lambda reference: (
            reference.provider,
            reference.resource_type,
            reference.provider_resource_identifier,
            reference.source_version,
            reference.content_hash,
        ),
    )
    return sha256_json(
        {
            "workflow_version": WORKFLOW_VERSION,
            "decision": {
                "outcome": decision.outcome.value,
                "reason_codes": sorted(code.value for code in decision.reason_codes),
                "policy_version": decision.policy_version,
            },
            "sources": [
                {
                    "provider": reference.provider,
                    "resource_type": reference.resource_type,
                    "provider_resource_identifier": reference.provider_resource_identifier,
                    "source_version": reference.source_version,
                    "content_hash": reference.content_hash,
                }
                for reference in references
            ],
            "authoritative_github": {
                "provider_resource_identifier": (github_action.provider_resource_identifier),
                "revision": github_action.revision,
            },
            "slack_team": expected_team_id,
            "channel": channel_id,
        }
    )


def _identity(payload: dict[str, JsonValue]) -> SlackIdentity:
    try:
        return SlackIdentity(
            team_id=_required_string(payload, "team_id"),
            user_id=_required_string(payload, "user_id"),
            bot_id=_optional_string(payload, "bot_id"),
        )
    except ValueError:
        raise SlackPermanentFailure("slack_auth_malformed") from None


def _slack_api_error(
    payload: dict[str, JsonValue],
    *,
    identity: bool,
) -> SlackError:
    raw_error = payload.get("error")
    error = raw_error if isinstance(raw_error, str) else "unknown_error"
    if error == "ratelimited":
        return SlackRetryableFailure("slack_rate_limited")
    if identity:
        return SlackPermanentFailure("slack_auth_rejected")
    safe_codes = {
        "invalid_auth": "slack_auth_rejected",
        "not_authed": "slack_auth_rejected",
        "token_revoked": "slack_auth_rejected",
        "missing_scope": "slack_missing_scope",
        "channel_not_found": "slack_channel_not_found",
        "not_in_channel": "slack_bot_not_in_channel",
        "is_archived": "slack_channel_archived",
        "restricted_action": "slack_restricted_action",
    }
    return SlackPermanentFailure(safe_codes.get(error, "slack_write_rejected"))


def _object(value: JsonValue | None, *, error: SlackError) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise error
    return value


def _required_string(payload: Mapping[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("required Slack field is invalid")
    return value


def _optional_string(payload: Mapping[str, JsonValue], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("optional Slack field is invalid")
    return value


def _retry_after(headers: Mapping[str, str], *, maximum: float) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), maximum)
    except ValueError:
        return None


def _known_unsent_delay(error: SlackRetryableFailure) -> float:
    """Prevent busy-loop retries when Slack supplies no usable Retry-After value."""
    if error.retry_after_seconds is None:
        return _DEFAULT_RETRY_DELAY_SECONDS
    return max(1.0, error.retry_after_seconds)


def _action_reference(action: ExternalAction) -> ExternalActionReference:
    provider_identifier = action.provider_resource_identifier
    if (
        ExternalActionStatus(action.status) is ExternalActionStatus.SUCCEEDED
        and provider_identifier is None
    ):
        raise ExecutionInvariantError("successful external action has no provider identifier")
    return ExternalActionReference(
        action_id=action.id,
        provider=action.provider,
        operation=action.operation,
        revision=action.revision,
        provider_resource_identifier=provider_identifier,
        provider_url=action.provider_url if provider_identifier is not None else None,
    )
