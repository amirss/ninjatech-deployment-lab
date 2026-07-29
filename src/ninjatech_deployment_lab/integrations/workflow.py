from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import ValidationError

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.connectors import (
    GitHubComment,
    GitHubReaderWriter,
    JiraReader,
    PermanentProviderError,
    ProviderWriteOutcomeUnknown,
    RetryableProviderError,
    ServiceCatalogReader,
    safe_marker,
)
from ninjatech_deployment_lab.integrations.domain import (
    WORKFLOW_VERSION,
    DataClassification,
    DecisionOutcome,
    DecisionReasonCode,
    DeploymentContextDecision,
    DeploymentContextResult,
    DeploymentContextSyncInput,
    ExternalActionReference,
    GitHubRepositoryContext,
    JiraWorkItem,
    ServiceCatalogRecord,
    SlackDeliveryState,
    SlackNotificationResult,
    SourceReference,
    sha256_json,
)
from ninjatech_deployment_lab.integrations.metrics import (
    MetricLabel,
    MetricName,
    MetricsSink,
    StructuredLoggingMetricsSink,
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
    SourceArtifactRepository,
    source_reference,
)
from ninjatech_deployment_lab.integrations.policy import (
    PolicyEvaluation,
    evaluate_service_catalog,
    evaluate_static_scope,
)
from ninjatech_deployment_lab.integrations.rendering import render_github_comment
from ninjatech_deployment_lab.integrations.slack import (
    SlackDeliveryRequest,
    SlackDeliveryService,
)
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import (
    ExecutionInvariantError,
    PermanentTaskError,
    RetryableTaskError,
)
from ninjatech_deployment_lab.worker.handlers import HandlerContext, TaskExecution

logger = logging.getLogger(__name__)
ProviderResult = TypeVar("ProviderResult")


class DeploymentContextSyncHandler:
    """Policy-first deterministic workflow with one authoritative GitHub side effect."""

    def __init__(
        self,
        *,
        settings: Settings,
        service_catalog: ServiceCatalogReader,
        jira: JiraReader,
        github: GitHubReaderWriter,
        artifacts: SourceArtifactRepository,
        actions: ExternalActionRepository,
        metrics: MetricsSink | None = None,
        slack_delivery_factory: Callable[[], SlackDeliveryService] | None = None,
    ) -> None:
        self._settings = settings
        self._service_catalog = service_catalog
        self._jira = jira
        self._github = github
        self._artifacts = artifacts
        self._actions = actions
        self._metrics = metrics or StructuredLoggingMetricsSink()
        self._slack_delivery_factory = slack_delivery_factory
        self._slack_delivery: SlackDeliveryService | None = None

    async def execute(
        self,
        task: TaskExecution,
        context: HandlerContext,
    ) -> dict[str, JsonValue]:
        try:
            task_input = DeploymentContextSyncInput.model_validate(task.task_input)
        except ValidationError:
            raise PermanentTaskError("deployment_context_input_invalid") from None
        self._validate_slack_request(task_input)
        context.raise_if_cancelled()

        static_result = evaluate_static_scope(task_input, self._settings)
        if static_result is not None:
            self._log_decision(task, static_result)
            result = self._result(
                evaluation=static_result,
                references=(),
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )

        records = await self._provider_call(
            self._service_catalog.fetch_records(
                task_input.service_id,
                correlation_id=str(task.task_id),
            )
        )
        context.raise_if_cancelled()
        catalog_evaluation = evaluate_service_catalog(task_input, records, self._settings)
        self._log_decision(task, catalog_evaluation)
        catalog_references = await self._record_catalog(
            task=task,
            records=records,
            selected=catalog_evaluation.record,
        )
        if catalog_evaluation.outcome is not DecisionOutcome.READY:
            result = self._result(
                evaluation=catalog_evaluation,
                references=catalog_references,
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )
        service = catalog_evaluation.record
        if service is None:
            raise PermanentTaskError("service_catalog_contract_error")

        context.raise_if_cancelled()
        identity_verified = await self._provider_call(
            self._github.verify_identity(correlation_id=str(task.task_id))
        )
        if not identity_verified:
            result = self._review_result(
                evaluation=catalog_evaluation,
                references=catalog_references,
                code=DecisionReasonCode.PROVIDER_IDENTITY_UNVERIFIED,
                reason="The configured GitHub identity could not be verified.",
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )

        github = await self._provider_call(
            self._github.fetch_context(
                task_input.github_repository,
                task_input.github_issue_number,
                correlation_id=str(task.task_id),
            )
        )
        context.raise_if_cancelled()
        if github.full_name.casefold() != task_input.github_repository.casefold():
            result = self._review_result(
                evaluation=catalog_evaluation,
                references=catalog_references,
                code=DecisionReasonCode.GITHUB_REPOSITORY_IDENTITY_MISMATCH,
                reason="GitHub returned a repository other than the requested repository.",
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )
        if github.issue_number != task_input.github_issue_number:
            result = self._review_result(
                evaluation=catalog_evaluation,
                references=catalog_references,
                code=DecisionReasonCode.GITHUB_ISSUE_IDENTITY_MISMATCH,
                reason="GitHub returned an issue other than the requested issue.",
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )
        github_reference = await self._record_github(task, github)
        references = (*catalog_references, github_reference)
        if github.is_pull_request:
            result = self._review_result(
                evaluation=catalog_evaluation,
                references=references,
                code=DecisionReasonCode.TARGET_IS_PULL_REQUEST,
                reason="Pull requests are not valid targets for deployment-context comments.",
                generated_at=datetime.now(UTC),
                outcome=DecisionOutcome.BLOCKED,
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )
        if github.archived:
            result = self._review_result(
                evaluation=catalog_evaluation,
                references=references,
                code=DecisionReasonCode.REPOSITORY_ARCHIVED,
                reason="The authoritative GitHub repository is archived.",
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )
        if github.issue_state.casefold() != "open":
            result = self._review_result(
                evaluation=catalog_evaluation,
                references=references,
                code=DecisionReasonCode.ISSUE_CLOSED,
                reason="The target GitHub issue is not open.",
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )

        context.raise_if_cancelled()
        jira = await self._provider_call(
            self._jira.fetch_issue(
                task_input.jira_issue_key,
                correlation_id=str(task.task_id),
            )
        )
        context.raise_if_cancelled()
        if jira.key.casefold() != task_input.jira_issue_key.casefold():
            result = self._review_result(
                evaluation=catalog_evaluation,
                references=references,
                code=DecisionReasonCode.JIRA_ISSUE_IDENTITY_MISMATCH,
                reason="Jira returned a work item other than the requested issue.",
                generated_at=datetime.now(UTC),
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
            )
        jira_reference = await self._record_jira(task, jira, service.data_classification)
        references = (*references, jira_reference)
        decision = DeploymentContextDecision(
            outcome=DecisionOutcome.READY,
            reason_codes=catalog_evaluation.reason_codes,
            reasons=catalog_evaluation.reasons,
            source_references=references,
            policy_version=service.deployment_policy_version,
            generated_at=jira.updated_at,
        )
        action_scope_key = self._action_scope_key(task_input, github, service)
        comment_body = render_github_comment(
            action_scope_key=action_scope_key,
            decision=decision,
            service=service,
            github=github,
            jira=jira,
        )
        desired_fingerprint = sha256_json({"body": comment_body})
        snapshot_hash = self._decision_snapshot_hash(decision)
        context.raise_if_cancelled()
        try:
            reservation = await self._actions.reserve_or_get(
                fence=task.execution_fence,
                provider=ExternalActionProvider.GITHUB,
                operation=ExternalActionOperation.UPSERT_DEPLOYMENT_CONTEXT_COMMENT,
                action_scope_key=action_scope_key,
                desired_request_fingerprint=desired_fingerprint,
                decision_snapshot_hash=snapshot_hash,
            )
        except ExecutionInvariantError:
            context.raise_if_cancelled()
            raise

        action = reservation.action
        if reservation.kind is ActionReservationKind.SOURCE_DRIFT:
            result = self._action_review_result(
                decision,
                references,
                action,
                DecisionReasonCode.SOURCE_VERSION_DRIFT,
                "Source evidence changed after this action scope was reserved.",
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
                github=github,
                service=service,
            )
        if reservation.kind is ActionReservationKind.CHANGED:
            action_or_result = await self._prepare_controlled_revision(
                task=task,
                context=context,
                action=action,
                service=service,
                repository=task_input.github_repository,
                desired_fingerprint=desired_fingerprint,
                snapshot_hash=snapshot_hash,
                decision=decision,
                references=references,
            )
            if isinstance(action_or_result, dict):
                return await self._finish(
                    result=action_or_result,
                    task_input=task_input,
                    task=task,
                    context=context,
                    github=github,
                    service=service,
                )
            action = action_or_result
        elif (
            reservation.kind is ActionReservationKind.REPLAY
            and ExternalActionStatus(action.status) is ExternalActionStatus.SUCCEEDED
        ):
            return await self._finish(
                result=self._success_result(decision, references, action),
                task_input=task_input,
                task=task,
                context=context,
                github=github,
                service=service,
            )
        elif reservation.kind is ActionReservationKind.REPLAY and ExternalActionStatus(
            action.status
        ) in {
            ExternalActionStatus.NEEDS_HUMAN_REVIEW,
            ExternalActionStatus.PERMANENT_FAILURE,
        }:
            result = self._action_review_result(
                decision,
                references,
                action,
                DecisionReasonCode.RECONCILIATION_INCONCLUSIVE,
                "The existing authoritative action requires human resolution.",
            )
            return await self._finish(
                result=result,
                task_input=task_input,
                task=task,
                context=context,
                github=github,
                service=service,
            )

        result = await self._reconcile_and_apply(
            task=task,
            context=context,
            action=action,
            repository=task_input.github_repository,
            issue_number=task_input.github_issue_number,
            body=comment_body,
            desired_fingerprint=desired_fingerprint,
            decision=decision,
            references=references,
        )
        return await self._finish(
            result=result,
            task_input=task_input,
            task=task,
            context=context,
            github=github,
            service=service,
        )

    def _validate_slack_request(self, task_input: DeploymentContextSyncInput) -> None:
        if not task_input.publish_slack_notification:
            return
        if not self._settings.enable_slack_notification:
            raise PermanentTaskError("slack_notification_not_enabled")
        channel_id = task_input.slack_channel_id
        if channel_id is None:
            raise PermanentTaskError("slack_channel_required")
        if channel_id not in self._settings.deployment_allowed_slack_channels:
            raise PermanentTaskError("slack_channel_not_allowed")
        if self._slack_delivery_factory is None:
            raise PermanentTaskError("slack_delivery_not_configured")

    async def _finish(
        self,
        *,
        result: dict[str, JsonValue],
        task_input: DeploymentContextSyncInput,
        task: TaskExecution,
        context: HandlerContext,
        github: GitHubRepositoryContext | None = None,
        service: ServiceCatalogRecord | None = None,
    ) -> dict[str, JsonValue]:
        parsed = DeploymentContextResult.model_validate(result)
        if not task_input.publish_slack_notification:
            return parsed.model_dump(mode="json")
        github_action = parsed.authoritative_github_action
        channel_id = task_input.slack_channel_id
        if (
            parsed.decision.outcome is not DecisionOutcome.READY
            or github_action is None
            or github_action.provider_resource_identifier is None
            or github is None
            or service is None
            or channel_id is None
        ):
            return parsed.model_copy(
                update={
                    "secondary_slack_notification": SlackNotificationResult(
                        requested=True,
                        state=SlackDeliveryState.NEEDS_HUMAN_REVIEW,
                        safe_error_code="slack_authoritative_action_unavailable",
                    )
                }
            ).model_dump(mode="json")

        context.raise_if_cancelled()
        if self._slack_delivery is None:
            if self._slack_delivery_factory is None:
                raise PermanentTaskError("slack_delivery_not_configured")
            self._slack_delivery = self._slack_delivery_factory()
        slack_result = await self._slack_delivery.deliver(
            task=task,
            context=context,
            request=SlackDeliveryRequest(
                channel_id=channel_id,
                canonical_service_id=service.canonical_service_id,
                github_repository_id=github.repository_id,
                github_issue_number=github.issue_number,
                github_action=github_action,
                decision=parsed.decision,
            ),
        )
        return parsed.model_copy(update={"secondary_slack_notification": slack_result}).model_dump(
            mode="json"
        )

    async def _reconcile_and_apply(
        self,
        *,
        task: TaskExecution,
        context: HandlerContext,
        action: ExternalAction,
        repository: str,
        issue_number: int,
        body: str,
        desired_fingerprint: str,
        decision: DeploymentContextDecision,
        references: tuple[SourceReference, ...],
    ) -> dict[str, JsonValue]:
        status = ExternalActionStatus(action.status)
        if status not in {
            ExternalActionStatus.RECONCILING,
            ExternalActionStatus.READY_TO_EXECUTE,
        }:
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={
                    ExternalActionStatus.RESERVED,
                    ExternalActionStatus.RETRYABLE_FAILURE,
                    ExternalActionStatus.OUTCOME_UNKNOWN,
                    ExternalActionStatus.EXECUTING,
                },
                new_status=ExternalActionStatus.RECONCILING,
                transition="reconciliation_started",
            )
        context.raise_if_cancelled()
        try:
            existing = await self._reconcile_comment(
                action=action,
                repository=repository,
                issue_number=issue_number,
                marker=safe_marker(action.action_scope_key),
                correlation_id=str(task.task_id),
            )
        except RetryableProviderError as error:
            await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.RECONCILING},
                new_status=ExternalActionStatus.RETRYABLE_FAILURE,
                transition="reconciliation_retryable_failure",
                error_code=error.error_code,
            )
            raise RetryableTaskError(
                error.error_code,
                retry_after_seconds=error.retry_after_seconds,
            ) from None
        except ReconciliationNeedsReview as error:
            action = await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.RECONCILING},
                new_status=ExternalActionStatus.NEEDS_HUMAN_REVIEW,
                transition="reconciliation_needs_human_review",
                values={"completed_at": None},
                error_code=error.code.value,
            )
            return self._action_review_result(
                decision,
                references,
                action,
                error.code,
                error.safe_reason,
            )

        if existing is not None:
            if sha256_json({"body": existing.body}) != desired_fingerprint:
                if not (
                    action.revision > 1
                    and action.applied_request_fingerprint is not None
                    and action.applied_request_fingerprint != desired_fingerprint
                ):
                    action = await self._actions.transition(
                        fence=task.execution_fence,
                        action_id=action.id,
                        expected_statuses={ExternalActionStatus.RECONCILING},
                        new_status=ExternalActionStatus.NEEDS_HUMAN_REVIEW,
                        transition="reconciled_content_mismatch",
                        error_code=(DecisionReasonCode.RECONCILIATION_INCONCLUSIVE.value),
                    )
                    return self._action_review_result(
                        decision,
                        references,
                        action,
                        DecisionReasonCode.RECONCILIATION_INCONCLUSIVE,
                        "The matching GitHub comment does not contain the expected revision.",
                    )
                existing = None
            else:
                context.raise_if_ownership_lost()
                action = await self._complete_success(
                    task=task,
                    action=action,
                    comment=existing,
                    desired_fingerprint=desired_fingerprint,
                    expected={ExternalActionStatus.RECONCILING},
                    transition="reconciled_succeeded",
                )
                context.raise_if_cancelled()
                return self._success_result(decision, references, action)

        action = await self._actions.transition(
            fence=task.execution_fence,
            action_id=action.id,
            expected_statuses={ExternalActionStatus.RECONCILING},
            new_status=ExternalActionStatus.READY_TO_EXECUTE,
            transition="reconciliation_completed_no_match",
        )
        context.raise_if_cancelled()
        action = await self._actions.transition(
            fence=task.execution_fence,
            action_id=action.id,
            expected_statuses={ExternalActionStatus.READY_TO_EXECUTE},
            new_status=ExternalActionStatus.EXECUTING,
            transition="write_started",
            settlement_delay_seconds=self._settings.integration_settlement_delay_seconds,
        )
        context.raise_if_cancelled()
        try:
            if action.provider_resource_identifier is None:
                comment = await self._github.create_comment(
                    repository,
                    issue_number,
                    body,
                    correlation_id=str(task.task_id),
                )
            else:
                comment = await self._github.update_comment(
                    repository,
                    action.provider_resource_identifier,
                    body,
                    correlation_id=str(task.task_id),
                )
        except ProviderWriteOutcomeUnknown as error:
            await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.EXECUTING},
                new_status=ExternalActionStatus.OUTCOME_UNKNOWN,
                transition="write_outcome_unknown",
                error_code=error.error_code,
            )
            raise RetryableTaskError(error.error_code) from None
        except RetryableProviderError as error:
            await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.EXECUTING},
                new_status=ExternalActionStatus.RETRYABLE_FAILURE,
                transition="write_retryable_failure",
                error_code=error.error_code,
            )
            raise RetryableTaskError(
                error.error_code,
                retry_after_seconds=error.retry_after_seconds,
            ) from None
        except PermanentProviderError as error:
            await self._actions.transition(
                fence=task.execution_fence,
                action_id=action.id,
                expected_statuses={ExternalActionStatus.EXECUTING},
                new_status=ExternalActionStatus.PERMANENT_FAILURE,
                transition="write_permanent_failure",
                error_code=error.error_code,
            )
            raise PermanentTaskError(error.error_code) from None
        context.raise_if_ownership_lost()
        action = await self._complete_success(
            task=task,
            action=action,
            comment=comment,
            desired_fingerprint=desired_fingerprint,
            expected={ExternalActionStatus.EXECUTING},
            transition="write_succeeded",
        )
        context.raise_if_cancelled()
        return self._success_result(decision, references, action)

    async def _reconcile_comment(
        self,
        *,
        action: ExternalAction,
        repository: str,
        issue_number: int,
        marker: str,
        correlation_id: str,
    ) -> GitHubComment | None:
        if action.provider_resource_identifier is not None:
            comment = await self._github.get_comment(
                repository,
                action.provider_resource_identifier,
                correlation_id=correlation_id,
            )
            if comment is None:
                raise ReconciliationNeedsReview(
                    DecisionReasonCode.COMMENT_MISSING,
                    "A previously bound GitHub comment was deleted and will not be recreated.",
                )
            if marker not in comment.body:
                raise ReconciliationNeedsReview(
                    DecisionReasonCode.COMMENT_MARKER_MISSING,
                    "The previously bound GitHub comment no longer has its identity marker.",
                )
            return comment

        holdoff_seconds = (
            await self._actions.reconciliation_delay_seconds(action.id)
            if action.reconcile_not_before is not None
            else 0.0
        )
        if holdoff_seconds > 0:
            raise RetryableProviderError(
                "github_reconciliation_holdoff",
                retry_after_seconds=max(0.1, holdoff_seconds),
            )
        search = await self._github.find_comments_by_marker(
            repository,
            issue_number,
            marker,
            correlation_id=correlation_id,
        )
        if not search.complete:
            raise ReconciliationNeedsReview(
                DecisionReasonCode.RECONCILIATION_INCONCLUSIVE,
                "GitHub comment pagination was incomplete, so no write was attempted.",
            )
        if len(search.comments) > 1:
            raise ReconciliationNeedsReview(
                DecisionReasonCode.MULTIPLE_COMMENTS_FOUND,
                "Multiple GitHub comments share the authoritative action marker.",
            )
        return search.comments[0] if search.comments else None

    async def _prepare_controlled_revision(
        self,
        *,
        task: TaskExecution,
        context: HandlerContext,
        action: ExternalAction,
        service: ServiceCatalogRecord,
        repository: str,
        desired_fingerprint: str,
        snapshot_hash: str,
        decision: DeploymentContextDecision,
        references: tuple[SourceReference, ...],
    ) -> ExternalAction | dict[str, JsonValue]:
        if (
            not service.allow_automatic_updates
            or ExternalActionStatus(action.status) is not ExternalActionStatus.SUCCEEDED
            or action.provider_resource_identifier is None
        ):
            return self._action_review_result(
                decision,
                references,
                action,
                DecisionReasonCode.ACTION_SCOPE_CHANGED,
                "The authoritative action scope already has different applied content.",
            )
        context.raise_if_cancelled()
        existing = await self._provider_call(
            self._github.get_comment(
                repository,
                action.provider_resource_identifier,
                correlation_id=str(task.task_id),
            )
        )
        marker = safe_marker(action.action_scope_key)
        if existing is None or marker not in existing.body:
            return self._action_review_result(
                decision,
                references,
                action,
                DecisionReasonCode.COMMENT_MISSING,
                "The exact authoritative comment cannot be safely updated.",
            )
        context.raise_if_cancelled()
        return await self._actions.revise(
            fence=task.execution_fence,
            action_id=action.id,
            expected_revision=action.revision,
            desired_request_fingerprint=desired_fingerprint,
            decision_snapshot_hash=snapshot_hash,
        )

    async def _complete_success(
        self,
        *,
        task: TaskExecution,
        action: ExternalAction,
        comment: GitHubComment,
        desired_fingerprint: str,
        expected: set[ExternalActionStatus],
        transition: str,
    ) -> ExternalAction:
        return await self._actions.transition(
            fence=task.execution_fence,
            action_id=action.id,
            expected_statuses=expected,
            new_status=ExternalActionStatus.SUCCEEDED,
            transition=transition,
            values={
                "provider_resource_identifier": comment.identifier,
                "provider_url": comment.url,
                "applied_request_fingerprint": desired_fingerprint,
                "response_fingerprint": sha256_json(
                    {
                        "identifier": comment.identifier,
                        "url": comment.url,
                    }
                ),
            },
        )

    async def _record_catalog(
        self,
        *,
        task: TaskExecution,
        records: tuple[ServiceCatalogRecord, ...],
        selected: ServiceCatalogRecord | None,
    ) -> tuple[SourceReference, ...]:
        references: list[SourceReference] = []
        for record in records:
            restricted = record.data_classification in {
                DataClassification.CONFIDENTIAL,
                DataClassification.RESTRICTED,
            }
            payload: dict[str, JsonValue] = {
                "canonical_service_id": record.canonical_service_id,
                "data_classification": record.data_classification.value,
                "deployment_policy_version": record.deployment_policy_version,
                "source_version": record.source_version,
            }
            if not restricted:
                payload.update(
                    {
                        "service_owner": record.service_owner,
                        "criticality": record.criticality,
                        "approved_repositories": list(record.approved_repositories),
                        "automatic_publication_allowed": (record.automatic_publication_allowed),
                        "required_reviewer": record.required_reviewer,
                        "allow_automatic_updates": record.allow_automatic_updates,
                    }
                )
            artifact = await self._artifacts.record(
                fence=task.execution_fence,
                provider="service_catalog",
                resource_type="service",
                provider_resource_identifier=record.canonical_service_id,
                source_url=record.source_url,
                source_version=record.source_version,
                data_classification=record.data_classification,
                redaction_applied=restricted,
                normalized_payload=payload,
                fetched_at=datetime.now(UTC),
            )
            references.append(source_reference(artifact))
        return tuple(references)

    async def _record_github(
        self,
        task: TaskExecution,
        github: GitHubRepositoryContext,
    ) -> SourceReference:
        payload = github.model_dump(mode="json")
        artifact = await self._artifacts.record(
            fence=task.execution_fence,
            provider="github",
            resource_type="repository_issue_context",
            provider_resource_identifier=(f"{github.repository_id}:issue:{github.issue_number}"),
            source_url=github.source_url,
            source_version=github.source_version,
            data_classification=DataClassification.INTERNAL,
            redaction_applied=False,
            normalized_payload=payload,
            fetched_at=datetime.now(UTC),
        )
        return source_reference(artifact)

    async def _record_jira(
        self,
        task: TaskExecution,
        jira: JiraWorkItem,
        classification: DataClassification,
    ) -> SourceReference:
        restricted = classification in {
            DataClassification.CONFIDENTIAL,
            DataClassification.RESTRICTED,
        }
        payload = jira.model_dump(mode="json")
        if restricted:
            payload.pop("normalized_description_text", None)
        artifact = await self._artifacts.record(
            fence=task.execution_fence,
            provider="jira",
            resource_type="work_item",
            provider_resource_identifier=jira.key,
            source_url=jira.source_url,
            source_version=jira.source_version,
            data_classification=classification,
            redaction_applied=restricted,
            normalized_payload=payload,
            fetched_at=datetime.now(UTC),
        )
        return source_reference(artifact)

    def _action_scope_key(
        self,
        task_input: DeploymentContextSyncInput,
        github: GitHubRepositoryContext,
        service: ServiceCatalogRecord,
    ) -> str:
        scope = self._settings.deployment_scope_id
        if scope is None:
            raise PermanentTaskError("deployment_scope_not_configured")
        return (
            f"{WORKFLOW_VERSION}:{scope}:{github.repository_id}:"
            f"{task_input.github_issue_number}:{service.canonical_service_id}:github_comment"
        )

    def _log_decision(self, task: TaskExecution, evaluation: PolicyEvaluation) -> None:
        event = {
            DecisionOutcome.READY: "policy_decision_ready",
            DecisionOutcome.BLOCKED: "policy_decision_blocked",
            DecisionOutcome.NEEDS_HUMAN_REVIEW: "policy_decision_needs_review",
        }[evaluation.outcome]
        logger.info(
            event,
            extra={
                "event": event,
                "task_id": str(task.task_id),
                "attempt_id": str(task.attempt_id),
                "decision_reason_code": evaluation.reason_codes[0].value,
                "new_status": evaluation.outcome.value,
            },
        )
        self._metrics.increment(
            MetricName.POLICY_DECISION_COUNT,
            labels={MetricLabel.DECISION: evaluation.outcome},
        )

    @staticmethod
    def _decision_snapshot_hash(decision: DeploymentContextDecision) -> str:
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
                "policy_version": decision.policy_version,
                "outcome": decision.outcome.value,
                "reason_codes": [code.value for code in decision.reason_codes],
                "sources": [
                    {
                        "provider": reference.provider,
                        "resource_type": reference.resource_type,
                        "provider_resource_identifier": (reference.provider_resource_identifier),
                        "source_version": reference.source_version,
                        "content_hash": reference.content_hash,
                    }
                    for reference in references
                ],
            }
        )

    async def _provider_call(
        self,
        awaitable: Awaitable[ProviderResult],
    ) -> ProviderResult:
        try:
            return await awaitable
        except RetryableProviderError as error:
            raise RetryableTaskError(
                error.error_code,
                retry_after_seconds=error.retry_after_seconds,
            ) from None
        except PermanentProviderError as error:
            raise PermanentTaskError(error.error_code) from None

    @staticmethod
    def _result(
        *,
        evaluation: PolicyEvaluation,
        references: tuple[SourceReference, ...],
        generated_at: datetime,
    ) -> dict[str, JsonValue]:
        policy_version = (
            evaluation.record.deployment_policy_version if evaluation.record is not None else 1
        )
        result = DeploymentContextResult(
            decision=DeploymentContextDecision(
                outcome=evaluation.outcome,
                reason_codes=evaluation.reason_codes,
                reasons=evaluation.reasons,
                source_references=references,
                policy_version=policy_version,
                generated_at=generated_at,
            ),
            source_artifacts=references,
        )
        return result.model_dump(mode="json")

    @staticmethod
    def _review_result(
        *,
        evaluation: PolicyEvaluation,
        references: tuple[SourceReference, ...],
        code: DecisionReasonCode,
        reason: str,
        generated_at: datetime,
        outcome: DecisionOutcome = DecisionOutcome.NEEDS_HUMAN_REVIEW,
    ) -> dict[str, JsonValue]:
        decision = DeploymentContextDecision(
            outcome=outcome,
            reason_codes=(code,),
            reasons=(reason,),
            source_references=references,
            policy_version=(
                evaluation.record.deployment_policy_version if evaluation.record is not None else 1
            ),
            generated_at=generated_at,
        )
        return DeploymentContextResult(
            decision=decision,
            source_artifacts=references,
        ).model_dump(mode="json")

    @staticmethod
    def _action_review_result(
        decision: DeploymentContextDecision,
        references: tuple[SourceReference, ...],
        action: ExternalAction,
        code: DecisionReasonCode,
        reason: str,
    ) -> dict[str, JsonValue]:
        reviewed = decision.model_copy(
            update={
                "outcome": DecisionOutcome.NEEDS_HUMAN_REVIEW,
                "reason_codes": (code,),
                "reasons": (reason,),
            }
        )
        return DeploymentContextResult(
            decision=reviewed,
            source_artifacts=references,
            authoritative_github_action=_action_reference(action),
        ).model_dump(mode="json")

    @staticmethod
    def _success_result(
        decision: DeploymentContextDecision,
        references: tuple[SourceReference, ...],
        action: ExternalAction,
    ) -> dict[str, JsonValue]:
        return DeploymentContextResult(
            decision=decision,
            source_artifacts=references,
            authoritative_github_action=_action_reference(action),
        ).model_dump(mode="json")


class ReconciliationNeedsReview(Exception):
    def __init__(self, code: DecisionReasonCode, safe_reason: str) -> None:
        self.code = code
        self.safe_reason = safe_reason
        super().__init__("External action reconciliation requires human review")


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
