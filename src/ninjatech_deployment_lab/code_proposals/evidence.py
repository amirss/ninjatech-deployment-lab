from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from ninjatech_deployment_lab.code_proposals.domain import (
    EvidenceHandle,
    ModelCitationRequest,
    ProposalCitation,
)
from ninjatech_deployment_lab.integrations.domain import canonical_json


@dataclass(frozen=True, slots=True)
class SemanticEvidence:
    source_artifact_id: UUID
    provider: str
    resource_type: str
    provider_resource_identifier: str
    source_version: str
    content_hash: str
    normalized_text: str
    repository_path: str | None = None
    blob_sha: str | None = None

    def semantic_identity(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "resource_type": self.resource_type,
            "provider_resource_identifier": self.provider_resource_identifier,
            "source_version": self.source_version,
            "content_hash": self.content_hash,
            "repository_path": self.repository_path,
            "blob_sha": self.blob_sha,
        }


class EvidenceRegistry:
    """Deterministic model handles mapped to internal source-artifact evidence."""

    def __init__(self, evidence: tuple[SemanticEvidence, ...]) -> None:
        ordered = sorted(
            evidence,
            key=lambda item: canonical_json(item.semantic_identity()),
        )
        self._by_handle: dict[str, SemanticEvidence] = {}
        for item in ordered:
            full_hash = hashlib.sha256(canonical_json(item.semantic_identity())).hexdigest()
            length = 16
            while True:
                handle = f"E-{full_hash[:length]}"
                existing = self._by_handle.get(handle)
                if existing is None:
                    self._by_handle[handle] = item
                    break
                if existing.semantic_identity() == item.semantic_identity():
                    if existing.source_artifact_id != item.source_artifact_id:
                        # The semantic evidence is intentionally identical; retain one internal
                        # artifact mapping without changing the model-facing handle.
                        break
                    break
                length += 8
                if length > 64:
                    raise ValueError("evidence handle collision could not be resolved")

    def handles(self) -> tuple[EvidenceHandle, ...]:
        return tuple(sorted(self._by_handle))

    def items(self) -> tuple[tuple[EvidenceHandle, SemanticEvidence], ...]:
        return tuple((handle, self._by_handle[handle]) for handle in sorted(self._by_handle))

    def resolve(self, citation: ModelCitationRequest) -> ProposalCitation:
        evidence = self._by_handle.get(citation.evidence_handle)
        if evidence is None:
            raise ValueError("citation references an unknown evidence handle")
        if evidence.source_version != citation.source_version:
            raise ValueError("citation source version does not match verified evidence")
        if citation.repository_path != evidence.repository_path:
            raise ValueError("citation repository path does not match verified evidence")
        lines = _normalized_lines(evidence.normalized_text)
        if citation.end_line > len(lines):
            raise ValueError("citation line range exceeds verified evidence")
        excerpt = "\n".join(lines[citation.start_line - 1 : citation.end_line])
        excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        identity = canonical_json(evidence.semantic_identity())
        return ProposalCitation(
            source_artifact_id=evidence.source_artifact_id,
            semantic_source_identity=identity,
            repository_path=evidence.repository_path,
            source_version=evidence.source_version,
            start_line=citation.start_line,
            end_line=citation.end_line,
            content_hash=evidence.content_hash,
            cited_excerpt_hash=excerpt_hash,
        )

    def evidence_for_handle(self, handle: EvidenceHandle) -> SemanticEvidence:
        try:
            return self._by_handle[handle]
        except KeyError:
            raise ValueError("unknown evidence handle") from None


def _normalized_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
