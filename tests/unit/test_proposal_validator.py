from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.domain import (
    ProposedCodeChange,
    ValidatedCodeChange,
)
from ninjatech_deployment_lab.code_proposals.evidence import (
    EvidenceRegistry,
    SemanticEvidence,
)
from ninjatech_deployment_lab.code_proposals.repository import (
    ManifestEntry,
    RepositoryManifest,
)
from ninjatech_deployment_lab.code_proposals.validator import (
    ProposalValidationError,
    validate_proposal,
)


def _fixture() -> tuple[RepositoryManifest, EvidenceRegistry, dict[str, object]]:
    jira = SemanticEvidence(
        source_artifact_id=uuid4(),
        provider="jira",
        resource_type="work_item",
        provider_resource_identifier="ENG-123",
        source_version="jira-v1",
        content_hash="a" * 64,
        normalized_text="Change old to new.",
    )
    repository = SemanticEvidence(
        source_artifact_id=uuid4(),
        provider="github",
        resource_type="repository_file",
        provider_resource_identifier="7:src/app.py",
        source_version="b" * 40,
        content_hash="c" * 64,
        normalized_text="old",
        repository_path="src/app.py",
        blob_sha="b" * 40,
    )
    evidence = EvidenceRegistry((jira, repository))
    handles = {item.provider: handle for handle, item in evidence.items()}
    citations = [
        {
            "evidence_handle": handles["jira"],
            "repository_path": None,
            "source_version": "jira-v1",
            "start_line": 1,
            "end_line": 1,
        },
        {
            "evidence_handle": handles["github"],
            "repository_path": "src/app.py",
            "source_version": "b" * 40,
            "start_line": 1,
            "end_line": 1,
        },
    ]
    payload: dict[str, object] = {
        "proposal_version": "1",
        "base_commit_sha": "d" * 40,
        "jira_issue_key": "ENG-123",
        "jira_source_version": "jira-v1",
        "summary": "Update the behavior.",
        "assumptions": [],
        "file_changes": [
            {
                "path": "src/app.py",
                "change_type": "modify",
                "base_blob_sha": "b" * 40,
                "rationale": "Implement the requirement.",
                "unified_diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new",
                "citations": citations,
            }
        ],
        "test_intents": [
            {
                "kind": "regression",
                "target": "application behavior",
                "behavior": "Exercise the changed case.",
                "expected_result": "The new behavior is observed.",
                "trusted_test_profile_id": "python-unit",
            }
        ],
        "risk_flags": ["behavior_change"],
        "citations": citations,
        "model_confidence_band": "medium",
    }
    manifest = RepositoryManifest(
        repository_id=7,
        commit_sha="d" * 40,
        complete=True,
        entries=(
            ManifestEntry(
                repository_id=7,
                commit_sha="d" * 40,
                path="src/app.py",
                object_type="blob",
                mode="100644",
                blob_sha="b" * 40,
                byte_size=3,
                text_eligible=True,
            ),
        ),
    )
    return manifest, evidence, payload


def _validate(payload: dict[str, object]) -> ValidatedCodeChange:
    manifest, evidence, _ = _fixture()
    return validate_proposal(
        ProposedCodeChange.model_validate(payload),
        expected_base_commit_sha="d" * 40,
        expected_jira_issue_key="ENG-123",
        expected_jira_source_version="jira-v1",
        manifest=manifest,
        verified_file_contents={"src/app.py": "old"},
        evidence=evidence,
        budgets=ContextBudgets(),
        trusted_test_profile_ids=frozenset({"python-unit"}),
    )


def test_valid_proposal_translates_handles_to_public_citations() -> None:
    _, _, payload = _fixture()
    validated = _validate(payload)
    assert validated.proposal_fingerprint
    assert all(citation.source_artifact_id for citation in validated.citations)
    assert validated.file_changes[0].citations


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("base_commit_sha", "e" * 40, "base commit"),
        ("jira_issue_key", "ENG-999", "Jira key"),
        ("jira_source_version", "jira-v2", "Jira version"),
    ],
)
def test_authoritative_versions_must_match(field: str, value: str, reason: str) -> None:
    _, _, payload = _fixture()
    payload[field] = value
    with pytest.raises(ProposalValidationError, match=reason):
        _validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "migrations/0006.py",
        ".github/workflows/ci.yml",
        "Dockerfile",
        "compose.yaml",
        "infra/main.tf",
        "src/private.pem",
    ],
)
def test_forbidden_change_paths_never_validate(path: str) -> None:
    _, _, payload = _fixture()
    change = payload["file_changes"][0]  # type: ignore[index]
    change["path"] = path
    change["unified_diff"] = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new"
    with pytest.raises((ProposalValidationError, ValueError, ValidationError)):
        _validate(payload)


def test_missing_requirement_or_repository_citation_is_rejected() -> None:
    _, _, payload = _fixture()
    citations = payload["file_changes"][0]["citations"]  # type: ignore[index]
    for retained in (citations[:1], citations[1:]):
        copy = _fixture()[2]
        copy["file_changes"][0]["citations"] = retained  # type: ignore[index]
        with pytest.raises(ProposalValidationError, match="citation"):
            _validate(copy)


def test_context_mismatch_unknown_handle_and_secret_output_are_rejected() -> None:
    _, _, payload = _fixture()
    payload["file_changes"][0]["unified_diff"] = (  # type: ignore[index]
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-wrong\n+new"
    )
    with pytest.raises(ValueError, match="context"):
        _validate(payload)

    _, _, unknown = _fixture()
    unknown["file_changes"][0]["citations"][0]["evidence_handle"] = (  # type: ignore[index]
        "E-0000000000000000"
    )
    with pytest.raises(ValueError, match="unknown"):
        _validate(unknown)

    _, _, secret = _fixture()
    secret["summary"] = "ghp_" + "a" * 36
    with pytest.raises(ProposalValidationError, match="potential_secret_detected"):
        _validate(secret)


def test_unapproved_dependency_import_is_rejected() -> None:
    _, _, payload = _fixture()
    payload["file_changes"][0]["unified_diff"] = (  # type: ignore[index]
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+import vendor_sdk"
    )
    with pytest.raises(ProposalValidationError, match="dependency"):
        _validate(payload)
