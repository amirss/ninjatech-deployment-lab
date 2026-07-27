from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

WORKFLOW_VERSION = "deployment_context_sync:v1"
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


VisibleIdentifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=255, pattern=r"^[\x21-\x7E]+$"),
]


class DeploymentContextSyncInput(StrictModel):
    jira_issue_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,19}-[1-9][0-9]{0,11}$")
    github_repository: str = Field(min_length=3, max_length=200)
    github_issue_number: int = Field(ge=1, le=2_147_483_647)
    service_id: VisibleIdentifier
    publish_slack_notification: bool = False
    slack_channel_id: str | None = Field(
        default=None,
        pattern=r"^[CG][A-Z0-9]{8,30}$",
    )

    @field_validator("github_repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if REPOSITORY_PATTERN.fullmatch(value) is None:
            raise ValueError("github_repository must be owner/name")
        return value

    @model_validator(mode="after")
    def require_channel_for_notification(self) -> DeploymentContextSyncInput:
        if self.publish_slack_notification and self.slack_channel_id is None:
            raise ValueError("slack_channel_id is required when Slack publication is requested")
        return self


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class DecisionOutcome(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class DecisionReasonCode(StrEnum):
    READY = "ready"
    SERVICE_NOT_ALLOWED = "service_not_allowed"
    REPOSITORY_NOT_ALLOWED = "repository_not_allowed"
    REPOSITORY_NOT_OWNED_BY_SERVICE = "repository_not_owned_by_service"
    SERVICE_RECORD_CONFLICT = "service_record_conflict"
    SERVICE_RECORD_MALFORMED = "service_record_malformed"
    SERVICE_OWNER_MISSING = "service_owner_missing"
    POLICY_STALE = "policy_stale"
    DATA_CLASSIFICATION_BLOCKED = "data_classification_blocked"
    AUTOMATIC_PUBLICATION_DISABLED = "automatic_publication_disabled"
    REVIEWER_REQUIRED = "reviewer_required"
    REPOSITORY_ARCHIVED = "repository_archived"
    ISSUE_CLOSED = "issue_closed"
    PROVIDER_IDENTITY_UNVERIFIED = "provider_identity_unverified"
    SOURCE_DATA_INCOMPLETE = "source_data_incomplete"
    ACTION_SCOPE_CHANGED = "action_scope_changed"
    SOURCE_VERSION_DRIFT = "source_version_drift"
    RECONCILIATION_INCONCLUSIVE = "reconciliation_inconclusive"
    COMMENT_MISSING = "comment_missing"
    COMMENT_MARKER_MISSING = "comment_marker_missing"
    MULTIPLE_COMMENTS_FOUND = "multiple_comments_found"


class SourceReference(StrictModel):
    artifact_id: UUID
    provider: str
    resource_type: str
    provider_resource_identifier: str
    source_version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class JiraWorkItem(StrictModel):
    key: str
    title: str = Field(max_length=500)
    normalized_description_text: str = Field(max_length=20000)
    status: str = Field(max_length=100)
    priority: str | None = Field(default=None, max_length=100)
    labels: tuple[str, ...] = ()
    assignee_identifier: str | None = Field(default=None, max_length=255)
    updated_at: datetime
    source_url: str
    source_version: str


class GitHubRepositoryContext(StrictModel):
    full_name: str
    repository_id: int = Field(ge=1)
    visibility: str
    archived: bool
    default_branch: str
    default_branch_head_sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    issue_number: int = Field(ge=1)
    issue_state: str
    issue_title: str = Field(max_length=500)
    source_url: str
    source_version: str


class ServiceCatalogRecord(StrictModel):
    service_id: str
    canonical_service_id: str
    aliases: tuple[str, ...] = ()
    service_owner: str | None = None
    criticality: str
    approved_repositories: tuple[str, ...]
    data_classification: DataClassification
    deployment_policy_version: int = Field(ge=1)
    automatic_publication_allowed: bool
    required_reviewer: str | None = None
    allow_automatic_updates: bool = False
    source_version: str
    source_url: str


class DeploymentContextDecision(StrictModel):
    outcome: DecisionOutcome
    reason_codes: tuple[DecisionReasonCode, ...]
    reasons: tuple[str, ...]
    source_references: tuple[SourceReference, ...]
    policy_version: int
    generated_at: datetime


class ExternalActionReference(StrictModel):
    action_id: UUID
    provider: str
    operation: str
    revision: int
    provider_resource_identifier: str
    provider_url: str | None = None


class DeploymentContextResult(StrictModel):
    workflow_version: str = WORKFLOW_VERSION
    decision: DeploymentContextDecision
    source_artifacts: tuple[SourceReference, ...]
    authoritative_github_action: ExternalActionReference | None = None


def canonical_json(value: object) -> bytes:
    """Serialize finite JSON deterministically for hashes and size limits."""
    _reject_non_finite(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON must not contain NaN or Infinity")
    if isinstance(value, list | tuple):
        for item in value:
            _reject_non_finite(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _reject_non_finite(item)


def canonical_source_url(value: str) -> str:
    """Remove query/fragment/userinfo from a safe source URL."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must be absolute HTTP(S)")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", parsed.path, "", ""))
