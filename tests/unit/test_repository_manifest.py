from __future__ import annotations

import pytest

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.repository import (
    ManifestEntry,
    ManifestValidationError,
    RepositoryManifest,
    validate_manifest,
    validate_repository_path,
)


def _entry(path: str, *, object_type: str = "blob", mode: str = "100644") -> ManifestEntry:
    return ManifestEntry(
        repository_id=7,
        commit_sha="a" * 40,
        path=path,
        object_type=object_type,
        mode=mode,
        blob_sha="b" * 40 if object_type == "blob" else None,
        byte_size=10 if object_type == "blob" else None,
        text_eligible=True,
    )


def _manifest(*entries: ManifestEntry, complete: bool = True) -> RepositoryManifest:
    return RepositoryManifest(
        repository_id=7,
        commit_sha="a" * 40,
        complete=complete,
        entries=entries,
    )


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret",
        "src/../secret",
        "src\\file.py",
        ".git/config",
        "node_modules/pkg/index.js",
        "src/private.pem",
        "src/app.key",
        "src/secret_value.py",
        "build/output.py",
        "src/\x00bad.py",
    ],
)
def test_forbidden_repository_paths_are_rejected(path: str) -> None:
    with pytest.raises(ManifestValidationError):
        validate_repository_path(path)


def test_incomplete_binary_symlink_and_oversized_manifest_entries_are_rejected() -> None:
    budgets = ContextBudgets(maximum_bytes_per_file=8)
    with pytest.raises(ManifestValidationError, match="incomplete"):
        validate_manifest(_manifest(_entry("src/app.py"), complete=False), budgets=budgets)
    binary = _entry("src/app.py").model_copy(update={"text_eligible": False})
    with pytest.raises(ManifestValidationError, match="binary"):
        validate_manifest(_manifest(binary), budgets=ContextBudgets())
    with pytest.raises(ValueError):
        _entry("src/link", mode="120000")
    with pytest.raises(ManifestValidationError, match="size"):
        validate_manifest(_manifest(_entry("src/app.py")), budgets=budgets)


def test_case_and_unicode_normalization_collisions_are_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="ambiguous"):
        validate_manifest(
            _manifest(_entry("src/App.py"), _entry("src/app.py")),
            budgets=ContextBudgets(),
        )
    decomposed = "src/cafe\u0301.py"
    with pytest.raises(ManifestValidationError):
        validate_manifest(_manifest(_entry(decomposed)), budgets=ContextBudgets())


def test_file_directory_identity_collision_is_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="file and directory"):
        validate_manifest(
            _manifest(_entry("src/pkg"), _entry("src/pkg/app.py")),
            budgets=ContextBudgets(),
        )
