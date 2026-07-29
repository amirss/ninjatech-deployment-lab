from __future__ import annotations

import fnmatch
import unicodedata
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.domain import GitObjectSha, RepositoryPath


class RepositoryObjectType(StrEnum):
    BLOB = "blob"
    TREE = "tree"


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: int = Field(gt=0)
    commit_sha: GitObjectSha
    path: RepositoryPath
    object_type: RepositoryObjectType
    mode: str
    blob_sha: GitObjectSha | None = None
    byte_size: int | None = Field(default=None, ge=0)
    text_eligible: bool

    @model_validator(mode="after")
    def validate_object(self) -> ManifestEntry:
        if self.object_type is RepositoryObjectType.BLOB:
            if self.mode != "100644" or self.blob_sha is None or self.byte_size is None:
                raise ValueError("manifest blob must be a regular file with SHA and size")
        elif self.blob_sha is not None or self.byte_size is not None:
            raise ValueError("manifest tree must not claim blob metadata")
        return self


class RepositoryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: int = Field(gt=0)
    commit_sha: GitObjectSha
    complete: bool
    entries: tuple[ManifestEntry, ...]


class ManifestValidationError(ValueError):
    pass


_DENIED_SEGMENTS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "coverage",
        "__pycache__",
    }
)
_DENIED_PATTERNS = (
    ".env*",
    "*secret*",
    "*credential*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.min.js",
    "*.map",
)


def validate_manifest(
    manifest: RepositoryManifest,
    *,
    budgets: ContextBudgets,
) -> RepositoryManifest:
    if not manifest.complete:
        raise ManifestValidationError("repository manifest is incomplete")
    if len(manifest.entries) > budgets.maximum_manifest_entries:
        raise ManifestValidationError("repository manifest exceeds entry limit")
    canonical_paths: dict[str, tuple[str, RepositoryObjectType]] = {}
    exact_paths: set[str] = set()
    serialized_bytes = 0
    for entry in manifest.entries:
        if entry.repository_id != manifest.repository_id or entry.commit_sha != manifest.commit_sha:
            raise ManifestValidationError("manifest entry is not bound to the manifest identity")
        path = validate_repository_path(entry.path)
        serialized_bytes += len(path.encode("utf-8")) + 128
        if serialized_bytes > budgets.maximum_manifest_bytes:
            raise ManifestValidationError("repository manifest exceeds byte limit")
        if path in exact_paths:
            raise ManifestValidationError("duplicate manifest path")
        exact_paths.add(path)
        comparison = unicodedata.normalize("NFC", path).casefold()
        existing = canonical_paths.get(comparison)
        if existing is not None:
            raise ManifestValidationError("ambiguous Unicode or case-folded manifest path")
        canonical_paths[comparison] = (path, entry.object_type)
        if entry.object_type is RepositoryObjectType.BLOB:
            if not entry.text_eligible:
                raise ManifestValidationError("binary manifest entry is not allowed")
            if entry.byte_size is None or entry.byte_size > budgets.maximum_bytes_per_file:
                raise ManifestValidationError("manifest blob exceeds size limit")

    file_paths = {
        path
        for path, object_type in canonical_paths.values()
        if object_type is RepositoryObjectType.BLOB
    }
    for path, object_type in canonical_paths.values():
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in file_paths:
                raise ManifestValidationError("file and directory identities collide")
        if object_type is RepositoryObjectType.TREE and path in file_paths:
            raise ManifestValidationError("file and directory identities collide")
    return manifest


def validate_repository_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path:
        raise ManifestValidationError("repository path must be relative POSIX text")
    if unicodedata.normalize("NFC", path) != path:
        raise ManifestValidationError("repository path must use NFC normalization")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ManifestValidationError("repository path contains control characters")
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        raise ManifestValidationError("repository path is not valid UTF-8") from None
    if len(encoded) > 512:
        raise ManifestValidationError("repository path exceeds maximum length")
    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestValidationError("repository path contains traversal")
    lowered_parts = tuple(part.casefold() for part in parts)
    if any(part in _DENIED_SEGMENTS for part in lowered_parts):
        raise ManifestValidationError("repository path is generated, vendored, or internal")
    basename = parts[-1].casefold()
    full = path.casefold()
    if any(
        fnmatch.fnmatchcase(basename, pattern) or fnmatch.fnmatchcase(full, pattern)
        for pattern in _DENIED_PATTERNS
    ):
        raise ManifestValidationError("repository path may contain secret or generated material")
    return path


class RepositoryReader(Protocol):
    async def fetch_manifest(
        self,
        *,
        repository_id: int,
        commit_sha: GitObjectSha,
    ) -> RepositoryManifest: ...

    async def read_files(
        self,
        *,
        repository_id: int,
        commit_sha: GitObjectSha,
        entries: tuple[ManifestEntry, ...],
    ) -> tuple[str, ...]: ...
