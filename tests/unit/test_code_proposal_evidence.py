from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from ninjatech_deployment_lab.code_proposals import evidence as evidence_module
from ninjatech_deployment_lab.code_proposals.domain import ModelCitationRequest
from ninjatech_deployment_lab.code_proposals.evidence import (
    EvidenceRegistry,
    SemanticEvidence,
)


def _evidence(
    *,
    artifact_id: UUID | None = None,
    version: str = "v1",
    content_hash: str = "a" * 64,
) -> SemanticEvidence:
    return SemanticEvidence(
        source_artifact_id=artifact_id or uuid4(),
        provider="github",
        resource_type="repository_file",
        provider_resource_identifier="repo:src/app.py",
        source_version=version,
        content_hash=content_hash,
        normalized_text="first\nsecond\nthird",
        repository_path="src/app.py",
        blob_sha="b" * 40,
    )


def test_handles_ignore_artifact_uuid_and_input_order() -> None:
    first = _evidence(artifact_id=uuid4())
    same = _evidence(artifact_id=uuid4())
    other = SemanticEvidence(
        source_artifact_id=uuid4(),
        provider="jira",
        resource_type="work_item",
        provider_resource_identifier="ENG-123",
        source_version="jira-v1",
        content_hash="c" * 64,
        normalized_text="requirement",
    )
    one = EvidenceRegistry((first, other))
    two = EvidenceRegistry((other, same))

    assert one.handles() == two.handles()
    assert tuple(handle for handle, _ in one.items()) == tuple(handle for handle, _ in two.items())


def test_source_version_and_content_hash_change_handle() -> None:
    original = EvidenceRegistry((_evidence(),)).handles()[0]
    assert EvidenceRegistry((_evidence(version="v2"),)).handles()[0] != original
    assert EvidenceRegistry((_evidence(content_hash="d" * 64),)).handles()[0] != original


def test_unknown_handle_version_path_and_line_range_are_rejected() -> None:
    registry = EvidenceRegistry((_evidence(),))
    handle = registry.handles()[0]
    with pytest.raises(ValueError, match="unknown"):
        registry.resolve(
            ModelCitationRequest(
                evidence_handle="E-0000000000000000",
                repository_path="src/app.py",
                source_version="v1",
                start_line=1,
                end_line=1,
            )
        )
    for overrides in (
        {"source_version": "v2"},
        {"repository_path": "src/other.py"},
        {"end_line": 20},
    ):
        values = {
            "evidence_handle": handle,
            "repository_path": "src/app.py",
            "source_version": "v1",
            "start_line": 1,
            "end_line": 1,
            **overrides,
        }
        with pytest.raises(ValueError):
            registry.resolve(ModelCitationRequest(**values))


def test_public_citation_translation_and_excerpt_hash_are_deterministic() -> None:
    source = _evidence()
    registry = EvidenceRegistry((source,))
    request = ModelCitationRequest(
        evidence_handle=registry.handles()[0],
        repository_path="src/app.py",
        source_version="v1",
        start_line=2,
        end_line=3,
    )
    first = registry.resolve(request)
    second = registry.resolve(request)
    assert first == second
    assert first.source_artifact_id == source.source_artifact_id
    assert first.cited_excerpt_hash == hashlib.sha256(b"second\nthird").hexdigest()


def test_handle_prefix_collision_lengthens_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sha256 = hashlib.sha256

    class _Digest:
        def __init__(self, value: bytes) -> None:
            self._value = value

        def hexdigest(self) -> str:
            discriminator = "b" if b'"source_version":"v1"' in self._value else "c"
            return "a" * 16 + discriminator * 48

    module_hashlib = vars(evidence_module)["hashlib"]
    monkeypatch.setattr(module_hashlib, "sha256", _Digest)
    registry = EvidenceRegistry((_evidence(version="v1"), _evidence(version="v2")))
    assert registry.handles() == (
        "E-aaaaaaaaaaaaaaaa",
        "E-aaaaaaaaaaaaaaaacccccccc",
    )
    monkeypatch.setattr(module_hashlib, "sha256", real_sha256)
