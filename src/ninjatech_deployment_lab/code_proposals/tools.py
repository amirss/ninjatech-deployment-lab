from __future__ import annotations

from dataclasses import dataclass

from ninjatech_deployment_lab.code_proposals.context import BudgetLedger
from ninjatech_deployment_lab.code_proposals.domain import SearchPathsAction
from ninjatech_deployment_lab.code_proposals.repository import (
    ManifestEntry,
    RepositoryManifest,
    RepositoryObjectType,
)


@dataclass(frozen=True, slots=True)
class PathSearchResult:
    path: str
    blob_sha: str
    byte_size: int


def search_repository_paths(
    manifest: RepositoryManifest,
    action: SearchPathsAction,
    ledger: BudgetLedger,
) -> tuple[PathSearchResult, ...]:
    ledger.record_tool_call()
    lowered_terms = tuple(term.casefold() for term in action.terms)
    extensions = set(action.extensions)
    matches: list[ManifestEntry] = []
    for entry in manifest.entries:
        if entry.object_type is not RepositoryObjectType.BLOB:
            continue
        lowered_path = entry.path.casefold()
        if not all(term in lowered_path for term in lowered_terms):
            continue
        if extensions and not any(lowered_path.endswith(extension) for extension in extensions):
            continue
        matches.append(entry)
    matches.sort(key=lambda item: item.path)
    return tuple(
        PathSearchResult(
            path=entry.path,
            blob_sha=entry.blob_sha or "",
            byte_size=entry.byte_size or 0,
        )
        for entry in matches[: action.max_results]
    )
