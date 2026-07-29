from __future__ import annotations

import pytest

from ninjatech_deployment_lab.code_proposals.diff import (
    UnifiedDiffError,
    parse_unified_diff,
    validate_hunk_context,
)
from ninjatech_deployment_lab.code_proposals.domain import ChangeType


def test_parse_and_validate_modify_diff() -> None:
    parsed = parse_unified_diff(
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n old\n-value\n+new"
    )
    assert parsed.change_type is ChangeType.MODIFY
    validate_hunk_context(parsed, "old\nvalue")


def test_parse_and_validate_create_diff() -> None:
    parsed = parse_unified_diff("--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1,2 @@\n+one\n+two")
    assert parsed.change_type is ChangeType.CREATE
    validate_hunk_context(parsed, None)


@pytest.mark.parametrize(
    "value",
    [
        "diff --git a/src/a b/src/a\n--- a/src/a\n+++ b/src/a\n@@ -1 +1 @@\n-a\n+b",
        "--- a/src/a\n+++ b/src/b\n@@ -1 +1 @@\n-a\n+b",
        "--- a/src/a\n+++ b/src/a\nBinary files differ",
        "--- a/src/a\n+++ b/src/a\n@@ -1,2 +1 @@\n-a\n+b",
    ],
)
def test_forbidden_or_malformed_diffs_are_rejected(value: str) -> None:
    with pytest.raises(UnifiedDiffError):
        parse_unified_diff(value)


def test_hunk_context_must_match_verified_base() -> None:
    parsed = parse_unified_diff("--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new")
    with pytest.raises(UnifiedDiffError, match="context"):
        validate_hunk_context(parsed, "different")


def test_zero_count_insertion_cannot_start_beyond_verified_base() -> None:
    parsed = parse_unified_diff("--- a/src/app.py\n+++ b/src/app.py\n@@ -10,0 +11,1 @@\n+new")
    with pytest.raises(UnifiedDiffError, match="beyond"):
        validate_hunk_context(parsed, "one")
