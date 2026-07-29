from __future__ import annotations

import re
import sys
import unicodedata

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.diff import (
    ParsedUnifiedDiff,
    parse_unified_diff,
    validate_hunk_context,
)
from ninjatech_deployment_lab.code_proposals.domain import (
    ChangeType,
    FileChangeProposal,
    ProposalCitation,
    ProposedCodeChange,
    ValidatedCodeChange,
    ValidatedFileChangeProposal,
)
from ninjatech_deployment_lab.code_proposals.evidence import EvidenceRegistry
from ninjatech_deployment_lab.code_proposals.prompting import (
    canonical_json_bytes,
    sha256_canonical,
)
from ninjatech_deployment_lab.code_proposals.repository import (
    RepositoryManifest,
    RepositoryObjectType,
    validate_repository_path,
)
from ninjatech_deployment_lab.code_proposals.scanner import (
    ModelEgressScanner,
    PotentialSecretDetectedError,
)

_FORBIDDEN_EXACT = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "requirements.txt",
        "dockerfile",
        "compose.yaml",
        "docker-compose.yml",
    }
)
_FORBIDDEN_PREFIXES = (
    ".github/",
    "migrations/",
    "alembic/",
    "terraform/",
    "infra/",
    "infrastructure/",
    "k8s/",
    "kubernetes/",
)
_COMMAND_PATTERN = re.compile(
    r"(?im)(?:^|\n)\s*(?:bash|sh|zsh|python|python3|pytest|npm|npx|make|docker|git|uv)\s+"
)


class ProposalValidationError(ValueError):
    def __init__(self, safe_reason: str) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason


def validate_proposal(
    proposal: ProposedCodeChange,
    *,
    expected_base_commit_sha: str,
    expected_jira_issue_key: str,
    expected_jira_source_version: str,
    manifest: RepositoryManifest,
    verified_file_contents: dict[str, str],
    evidence: EvidenceRegistry,
    budgets: ContextBudgets,
    allowed_write_prefixes: tuple[str, ...] = ("src/", "tests/"),
    trusted_test_profile_ids: frozenset[str] = frozenset(),
) -> ValidatedCodeChange:
    serialized = canonical_json_bytes(proposal.model_dump(mode="json"))
    if len(serialized) > budgets.maximum_proposal_bytes:
        raise ProposalValidationError("proposal exceeds byte limit")
    if proposal.base_commit_sha != expected_base_commit_sha:
        raise ProposalValidationError("proposal base commit does not match verified source")
    if proposal.jira_issue_key != expected_jira_issue_key:
        raise ProposalValidationError("proposal Jira key does not match verified source")
    if proposal.jira_source_version != expected_jira_source_version:
        raise ProposalValidationError("proposal Jira version does not match verified source")
    if len(proposal.file_changes) > budgets.maximum_changed_files:
        raise ProposalValidationError("proposal exceeds changed-file limit")

    manifest_blobs = {
        entry.path: entry
        for entry in manifest.entries
        if entry.object_type is RepositoryObjectType.BLOB
    }
    comparison_paths = {
        unicodedata.normalize("NFC", entry.path).casefold() for entry in manifest.entries
    }
    proposed_comparisons: set[str] = set()
    translated_changes: list[ValidatedFileChangeProposal] = []
    total_diff_bytes = 0
    has_repository_evidence = any(item.repository_path is not None for _, item in evidence.items())

    for change in proposal.file_changes:
        _validate_change_path(change, allowed_write_prefixes)
        comparison = unicodedata.normalize("NFC", change.path).casefold()
        if comparison in proposed_comparisons:
            raise ProposalValidationError("proposal contains ambiguous changed paths")
        proposed_comparisons.add(comparison)
        if change.change_type is ChangeType.CREATE and comparison in comparison_paths:
            raise ProposalValidationError("created path collides with existing manifest")
        if change.change_type is ChangeType.MODIFY:
            entry = manifest_blobs.get(change.path)
            if entry is None or entry.blob_sha != change.base_blob_sha:
                raise ProposalValidationError("modified file blob does not match manifest")
        total_diff_bytes += len(change.unified_diff.encode("utf-8"))
        if total_diff_bytes > budgets.maximum_diff_bytes:
            raise ProposalValidationError("proposal exceeds diff-byte limit")
        parsed = parse_unified_diff(change.unified_diff)
        if parsed.path != change.path or parsed.change_type is not change.change_type:
            raise ProposalValidationError("diff does not match declared file change")
        validate_hunk_context(parsed, verified_file_contents.get(change.path))
        _reject_new_dependencies(parsed, manifest)
        translated = tuple(evidence.resolve(citation) for citation in change.citations)
        _require_file_citation_roles(translated, has_repository_evidence)
        _require_exact_modified_file_evidence(change, evidence)
        translated_changes.append(
            ValidatedFileChangeProposal(
                path=change.path,
                change_type=change.change_type,
                base_blob_sha=change.base_blob_sha,
                rationale=change.rationale,
                unified_diff=change.unified_diff,
                citations=translated,
            )
        )

    for intent in proposal.test_intents:
        values = (intent.target, intent.behavior, intent.expected_result)
        if any(_looks_executable(value) for value in values):
            raise ProposalValidationError("test intent contains executable command text")
        if (
            intent.trusted_test_profile_id is not None
            and intent.trusted_test_profile_id not in trusted_test_profile_ids
        ):
            raise ProposalValidationError("test intent uses an untrusted profile identifier")

    try:
        scanner = ModelEgressScanner()
        scanner.require_safe(proposal.summary)
        for assumption in proposal.assumptions:
            scanner.require_safe(assumption)
        for change in proposal.file_changes:
            scanner.require_safe(change.rationale)
            scanner.require_safe(change.unified_diff)
        for intent in proposal.test_intents:
            scanner.require_safe(intent.target)
            scanner.require_safe(intent.behavior)
            scanner.require_safe(intent.expected_result)
    except PotentialSecretDetectedError:
        raise ProposalValidationError("potential_secret_detected") from None
    translated_global = tuple(evidence.resolve(citation) for citation in proposal.citations)
    fingerprint = sha256_canonical(proposal.model_dump(mode="json"))
    return ValidatedCodeChange(
        proposal_version=proposal.proposal_version,
        base_commit_sha=proposal.base_commit_sha,
        jira_issue_key=proposal.jira_issue_key,
        jira_source_version=proposal.jira_source_version,
        summary=proposal.summary,
        assumptions=proposal.assumptions,
        file_changes=tuple(translated_changes),
        test_intents=proposal.test_intents,
        risk_flags=proposal.risk_flags,
        citations=translated_global,
        model_confidence_band=proposal.model_confidence_band,
        proposal_fingerprint=fingerprint,
        proposal_size_bytes=len(serialized),
    )


def _validate_change_path(
    change: FileChangeProposal,
    allowed_write_prefixes: tuple[str, ...],
) -> None:
    path = validate_repository_path(change.path)
    lowered = path.casefold()
    if lowered in _FORBIDDEN_EXACT or lowered.startswith(_FORBIDDEN_PREFIXES):
        raise ProposalValidationError("proposal targets a forbidden path")
    if not any(path.startswith(prefix) for prefix in allowed_write_prefixes):
        raise ProposalValidationError("proposal path is outside trusted write prefixes")


def _require_file_citation_roles(
    citations: tuple[ProposalCitation, ...],
    has_repository_evidence: bool,
) -> None:
    if not any('"provider":"jira"' in item.semantic_source_identity for item in citations):
        raise ProposalValidationError("changed file lacks requirement citation")
    if has_repository_evidence and not any(item.repository_path is not None for item in citations):
        raise ProposalValidationError("changed file lacks repository citation")


def _require_exact_modified_file_evidence(
    change: FileChangeProposal,
    evidence: EvidenceRegistry,
) -> None:
    if change.change_type is not ChangeType.MODIFY:
        return
    for citation in change.citations:
        source = evidence.evidence_for_handle(citation.evidence_handle)
        if source.repository_path == change.path and source.blob_sha == change.base_blob_sha:
            return
    raise ProposalValidationError("modified file lacks citation to its exact verified base blob")


def _looks_executable(value: str) -> bool:
    return bool(
        _COMMAND_PATTERN.search(value)
        or any(character in value for character in ("`", "$(", "&&", "||", ";"))
    )


def _reject_new_dependencies(
    parsed: ParsedUnifiedDiff,
    manifest: RepositoryManifest,
) -> None:
    added_lines = (
        line.text.strip() for hunk in parsed.hunks for line in hunk.lines if line.kind == "+"
    )
    internal_roots = {
        entry.path.split("/")[1]
        for entry in manifest.entries
        if entry.path.startswith("src/") and len(entry.path.split("/")) > 2
    }
    for line in added_lines:
        python_match = re.match(r"(?:from|import)\s+([A-Za-z_][A-Za-z0-9_.]*)", line)
        if python_match is not None:
            module = python_match.group(1)
            if module.startswith("."):
                continue
            root = module.split(".", maxsplit=1)[0]
            if root not in sys.stdlib_module_names and root not in internal_roots:
                raise ProposalValidationError("proposal introduces an unapproved dependency")
        javascript_match = re.search(
            r"(?:from\s+|require\()\s*['\"]([^'\"]+)['\"]",
            line,
        )
        if javascript_match is not None and not javascript_match.group(1).startswith("."):
            raise ProposalValidationError("proposal introduces an unapproved dependency")
