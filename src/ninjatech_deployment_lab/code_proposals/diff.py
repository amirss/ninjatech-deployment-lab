from __future__ import annotations

import re
from dataclasses import dataclass

from ninjatech_deployment_lab.code_proposals.domain import ChangeType
from ninjatech_deployment_lab.code_proposals.repository import validate_repository_path

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$"
)
_FORBIDDEN_HEADERS = (
    "diff --git ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "rename from ",
    "rename to ",
    "Binary files ",
    "GIT binary patch",
)


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True, slots=True)
class ParsedUnifiedDiff:
    path: str
    change_type: ChangeType
    hunks: tuple[DiffHunk, ...]


class UnifiedDiffError(ValueError):
    pass


def parse_unified_diff(value: str) -> ParsedUnifiedDiff:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if any(line.startswith(_FORBIDDEN_HEADERS) for line in lines):
        raise UnifiedDiffError("diff contains forbidden file metadata")
    if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
        raise UnifiedDiffError("diff must begin with old and new file headers")
    old_path = lines[0][4:]
    new_path = lines[1][4:]
    if "\t" in old_path or "\t" in new_path:
        raise UnifiedDiffError("diff headers must not contain timestamps")
    if old_path == "/dev/null":
        change_type = ChangeType.CREATE
        if not new_path.startswith("b/"):
            raise UnifiedDiffError("created file must use a b/ path")
        path = new_path[2:]
    else:
        change_type = ChangeType.MODIFY
        if not old_path.startswith("a/") or not new_path.startswith("b/"):
            raise UnifiedDiffError("modified file must use a/ and b/ paths")
        if old_path[2:] != new_path[2:]:
            raise UnifiedDiffError("rename is not allowed")
        path = new_path[2:]
    validate_repository_path(path)

    hunks: list[DiffHunk] = []
    index = 2
    while index < len(lines):
        match = _HUNK_HEADER.fullmatch(lines[index])
        if match is None:
            raise UnifiedDiffError("unexpected diff content outside a hunk")
        old_count = int(match.group("old_count") or "1")
        new_count = int(match.group("new_count") or "1")
        index += 1
        hunk_lines: list[DiffLine] = []
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line == r"\ No newline at end of file":
                index += 1
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise UnifiedDiffError("invalid unified-diff line")
            hunk_lines.append(DiffLine(line[0], line[1:]))
            index += 1
        actual_old = sum(line.kind in {" ", "-"} for line in hunk_lines)
        actual_new = sum(line.kind in {" ", "+"} for line in hunk_lines)
        if actual_old != old_count or actual_new != new_count:
            raise UnifiedDiffError("diff hunk counts do not match its body")
        hunks.append(
            DiffHunk(
                old_start=int(match.group("old_start")),
                old_count=old_count,
                new_start=int(match.group("new_start")),
                new_count=new_count,
                lines=tuple(hunk_lines),
            )
        )
    if not hunks:
        raise UnifiedDiffError("diff must contain at least one hunk")
    if change_type is ChangeType.CREATE and any(
        line.kind != "+" for hunk in hunks for line in hunk.lines
    ):
        raise UnifiedDiffError("created file diff may contain only added lines")
    return ParsedUnifiedDiff(path=path, change_type=change_type, hunks=tuple(hunks))


def validate_hunk_context(parsed: ParsedUnifiedDiff, base_content: str | None) -> None:
    if parsed.change_type is ChangeType.CREATE:
        if base_content is not None:
            raise UnifiedDiffError("created file unexpectedly has base content")
        for hunk in parsed.hunks:
            if hunk.old_start != 0 or hunk.old_count != 0:
                raise UnifiedDiffError("created file hunk must start from an empty base")
        return
    if base_content is None:
        raise UnifiedDiffError("modified file is missing verified base content")
    base_lines = base_content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    previous_end = 0
    for hunk in parsed.hunks:
        if hunk.old_start < 1 or hunk.old_start <= previous_end:
            raise UnifiedDiffError("diff hunks are overlapping or unordered")
        cursor = hunk.old_start - 1
        if cursor > len(base_lines):
            raise UnifiedDiffError("diff hunk begins beyond verified base content")
        for line in hunk.lines:
            if line.kind in {" ", "-"}:
                if cursor >= len(base_lines) or base_lines[cursor] != line.text:
                    raise UnifiedDiffError("diff context does not match verified base content")
                cursor += 1
        previous_end = hunk.old_start + hunk.old_count - 1
