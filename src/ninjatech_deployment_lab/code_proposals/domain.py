from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitObjectSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
EvidenceHandle = Annotated[str, StringConstraints(pattern=r"^E-[0-9a-f]{16,64}$")]
RepositoryPath = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
SafeReason = Annotated[str, StringConstraints(min_length=1, max_length=500)]
JiraIssueKey = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,19}-[1-9][0-9]{0,9}$"),
]
_EXECUTABLE_TEST_TEXT = re.compile(
    r"(?im)(?:^|\n)\s*(?:bash|sh|zsh|python|python3|pytest|npm|npx|make|docker|git|uv)\s+"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelProviderName(StrEnum):
    RECORDED = "recorded"
    OPENAI = "openai"


class ProposalOutcome(StrEnum):
    PROPOSED = "proposed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REFUSED = "refused"


class ChangeType(StrEnum):
    MODIFY = "modify"
    CREATE = "create"


class TestIntentKind(StrEnum):
    UNIT = "unit"
    INTEGRATION = "integration"
    REGRESSION = "regression"
    MANUAL = "manual"


class ModelConfidenceBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskFlag(StrEnum):
    BEHAVIOR_CHANGE = "behavior_change"
    DATA_HANDLING = "data_handling"
    ERROR_HANDLING = "error_handling"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    BACKWARD_COMPATIBILITY = "backward_compatibility"
    INSUFFICIENT_CONTEXT = "insufficient_context"


class HumanReviewReasonCode(StrEnum):
    INSUFFICIENT_CONTEXT = "insufficient_context"
    SOURCE_DRIFT = "source_drift"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    CONTEXT_BUDGET_EXHAUSTED = "context_budget_exhausted"
    POLICY_MISSING = "policy_missing"
    POLICY_CONFLICT = "policy_conflict"
    POLICY_STALE = "policy_stale"
    POLICY_REQUIRES_REVIEW = "policy_requires_review"
    POTENTIAL_SECRET_DETECTED = "potential_secret_detected"
    MANIFEST_INCOMPLETE = "manifest_incomplete"
    MANIFEST_AMBIGUOUS = "manifest_ambiguous"
    INVALID_PROPOSAL = "invalid_proposal"


class RefusalReasonCode(StrEnum):
    MODEL_PROCESSING_NOT_ALLOWED = "model_processing_not_allowed"
    DATA_CLASSIFICATION_NOT_ALLOWED = "data_classification_not_allowed"
    REPOSITORY_SOURCE_EGRESS_NOT_ALLOWED = "repository_source_egress_not_allowed"
    OUT_OF_SCOPE_REQUEST = "out_of_scope_request"
    MODEL_GUARDRAIL_VIOLATION = "model_guardrail_violation"


class EvidenceRole(StrEnum):
    POLICY_INPUT = "policy_input"
    REQUIREMENT_INPUT = "requirement_input"
    REPOSITORY_MANIFEST = "repository_manifest"
    REPOSITORY_FILE = "repository_file"
    PROPOSAL_CITATION = "proposal_citation"
    DRIFT_CHECK = "drift_check"


class ModelCitationRequest(StrictModel):
    evidence_handle: EvidenceHandle
    repository_path: RepositoryPath | None = None
    source_version: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered_line_range(self) -> ModelCitationRequest:
        if self.end_line < self.start_line:
            raise ValueError("citation end_line must not precede start_line")
        return self


class ProposalCitation(StrictModel):
    source_artifact_id: UUID
    semantic_source_identity: Annotated[str, StringConstraints(min_length=1, max_length=1200)]
    repository_path: RepositoryPath | None = None
    source_version: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: Sha256
    cited_excerpt_hash: Sha256


class FileChangeProposal(StrictModel):
    path: RepositoryPath
    change_type: ChangeType
    base_blob_sha: GitObjectSha | None = None
    rationale: BoundedText
    unified_diff: Annotated[str, StringConstraints(min_length=1, max_length=65536)]
    citations: tuple[ModelCitationRequest, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def require_correct_base_sha(self) -> FileChangeProposal:
        if self.change_type is ChangeType.MODIFY and self.base_blob_sha is None:
            raise ValueError("modify requires base_blob_sha")
        if self.change_type is ChangeType.CREATE and self.base_blob_sha is not None:
            raise ValueError("create must not provide base_blob_sha")
        return self


class TestIntent(StrictModel):
    kind: TestIntentKind
    target: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    behavior: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    expected_result: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    trusted_test_profile_id: (
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")] | None
    ) = None

    @field_validator("target", "behavior", "expected_result")
    @classmethod
    def reject_executable_text(cls, value: str) -> str:
        if _EXECUTABLE_TEST_TEXT.search(value) or any(
            character in value for character in ("`", "$(", "&&", "||", ";")
        ):
            raise ValueError("test intent must not contain executable command text")
        return value


class ProposedCodeChange(StrictModel):
    proposal_version: Literal["1"]
    base_commit_sha: GitObjectSha
    jira_issue_key: JiraIssueKey
    jira_source_version: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    summary: BoundedText
    assumptions: tuple[BoundedText, ...] = Field(default=(), max_length=20)
    file_changes: tuple[FileChangeProposal, ...] = Field(min_length=1, max_length=8)
    test_intents: tuple[TestIntent, ...] = Field(default=(), max_length=20)
    risk_flags: tuple[RiskFlag, ...] = Field(default=(), max_length=20)
    citations: tuple[ModelCitationRequest, ...] = Field(default=(), max_length=50)
    model_confidence_band: ModelConfidenceBand


class SearchPathsAction(StrictModel):
    action: Literal["search_repository_paths"]
    terms: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=100)],
        ...,
    ] = Field(min_length=1, max_length=5)
    extensions: tuple[
        Annotated[str, StringConstraints(pattern=r"^\.[a-z0-9]{1,15}$")],
        ...,
    ] = Field(default=(), max_length=20)
    max_results: int = Field(ge=1, le=50)


class ReadFileRequest(StrictModel):
    evidence_handle: EvidenceHandle
    repository_path: RepositoryPath


class ReadFilesAction(StrictModel):
    action: Literal["read_repository_files"]
    files: tuple[ReadFileRequest, ...] = Field(min_length=1, max_length=5)


class FinishProposalAction(StrictModel):
    action: Literal["finish"]
    proposal: ProposedCodeChange


class RequestHumanReviewAction(StrictModel):
    action: Literal["request_human_review"]
    reason_code: HumanReviewReasonCode
    reason: SafeReason
    citations: tuple[ModelCitationRequest, ...] = Field(default=(), max_length=20)


class RefuseAction(StrictModel):
    action: Literal["refuse"]
    reason_code: RefusalReasonCode
    reason: SafeReason
    citations: tuple[ModelCitationRequest, ...] = Field(default=(), max_length=20)


type ModelAction = Annotated[
    SearchPathsAction
    | ReadFilesAction
    | FinishProposalAction
    | RequestHumanReviewAction
    | RefuseAction,
    Field(discriminator="action"),
]


class SearchActionSummary(StrictModel):
    kind: Literal["search_repository_paths"]
    terms: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=100)],
        ...,
    ] = Field(min_length=1, max_length=5)
    extensions: tuple[
        Annotated[str, StringConstraints(pattern=r"^\.[a-z0-9]{1,15}$")],
        ...,
    ] = Field(default=(), max_length=20)
    max_results: int = Field(ge=1, le=50)


class ReadActionSummary(StrictModel):
    kind: Literal["read_repository_files"]
    requested_files: tuple[ReadFileRequest, ...] = Field(min_length=1, max_length=5)


class HumanReviewActionSummary(StrictModel):
    kind: Literal["request_human_review"]
    reason_code: HumanReviewReasonCode
    reason: SafeReason
    evidence_handles: tuple[EvidenceHandle, ...] = Field(default=(), max_length=20)


class RefusalActionSummary(StrictModel):
    kind: Literal["refuse"]
    reason_code: RefusalReasonCode
    reason: SafeReason
    evidence_handles: tuple[EvidenceHandle, ...] = Field(default=(), max_length=20)


class FinishActionSummary(StrictModel):
    kind: Literal["finish"]
    proposal_size_bytes: int = Field(ge=1, le=1048576)


type AgentActionSummary = Annotated[
    SearchActionSummary
    | ReadActionSummary
    | HumanReviewActionSummary
    | RefusalActionSummary
    | FinishActionSummary,
    Field(discriminator="kind"),
]


class PathSearchResultSummary(StrictModel):
    path: RepositoryPath
    blob_sha: GitObjectSha
    byte_size: int = Field(ge=0, le=1048576)


class PathSearchToolResultSummary(StrictModel):
    kind: Literal["path_search"]
    results: tuple[PathSearchResultSummary, ...] = Field(max_length=50)


class FileReadEvidenceSummary(StrictModel):
    evidence_handle: EvidenceHandle
    path: RepositoryPath
    blob_sha: GitObjectSha
    source_version: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    line_count: int = Field(ge=0, le=1000000)
    byte_size: int = Field(ge=0, le=1048576)
    content_hash: Sha256


class FileReadToolResultSummary(StrictModel):
    kind: Literal["file_read"]
    files: tuple[FileReadEvidenceSummary, ...] = Field(min_length=1, max_length=20)


type AgentToolResultSummary = Annotated[
    PathSearchToolResultSummary | FileReadToolResultSummary,
    Field(discriminator="kind"),
]


class ModelUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ModelEvidenceBlock(StrictModel):
    evidence_handle: EvidenceHandle
    evidence_kind: Literal[
        "policy",
        "requirement",
        "repository_manifest",
        "repository_file",
    ]
    source_version: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    repository_path: RepositoryPath | None = None
    blob_sha: GitObjectSha | None = None
    content_hash: Sha256
    untrusted_text: Annotated[str, StringConstraints(max_length=524288)]

    @model_validator(mode="after")
    def validate_repository_identity(self) -> ModelEvidenceBlock:
        is_repository_file = self.evidence_kind == "repository_file"
        if is_repository_file != (self.repository_path is not None and self.blob_sha is not None):
            raise ValueError("repository-file evidence requires path and blob SHA exclusively")
        return self


class ModelRequest(StrictModel):
    prompt_template_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    prompt_contract_hash: Sha256
    logical_step_number: int = Field(ge=1)
    completed_response_count: int = Field(ge=0)
    system_policy: str
    evidence_blocks: tuple[ModelEvidenceBlock, ...]
    prior_steps: tuple[dict[str, object], ...] = ()
    action_schema: dict[str, object]
    maximum_output_tokens: int = Field(ge=1)


class ModelResponse(StrictModel):
    action: ModelAction
    usage: ModelUsage = ModelUsage()
    response_fingerprint: Sha256
    response_size_bytes: int = Field(ge=1)


class ValidatedFileChangeProposal(StrictModel):
    path: RepositoryPath
    change_type: ChangeType
    base_blob_sha: GitObjectSha | None
    rationale: BoundedText
    unified_diff: str
    citations: tuple[ProposalCitation, ...]


class ValidatedCodeChange(StrictModel):
    proposal_version: Literal["1"]
    base_commit_sha: GitObjectSha
    jira_issue_key: JiraIssueKey
    jira_source_version: str
    summary: BoundedText
    assumptions: tuple[BoundedText, ...]
    file_changes: tuple[ValidatedFileChangeProposal, ...]
    test_intents: tuple[TestIntent, ...]
    risk_flags: tuple[RiskFlag, ...]
    citations: tuple[ProposalCitation, ...]
    model_confidence_band: ModelConfidenceBand
    proposal_fingerprint: Sha256
    proposal_size_bytes: int = Field(ge=1)
