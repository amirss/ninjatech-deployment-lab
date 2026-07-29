from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from ninjatech_deployment_lab.code_proposals.domain import (
    FinishActionSummary,
    FinishProposalAction,
    ModelAction,
    ProposedCodeChange,
)
from ninjatech_deployment_lab.code_proposals.domain import (
    TestIntent as ProposalTestIntent,
)


def _proposal() -> dict[str, object]:
    citation = {
        "evidence_handle": "E-1234567890abcdef",
        "repository_path": "src/app.py",
        "source_version": "v1",
        "start_line": 1,
        "end_line": 1,
    }
    return {
        "proposal_version": "1",
        "base_commit_sha": "a" * 40,
        "jira_issue_key": "ENG-123",
        "jira_source_version": "jira-v1",
        "summary": "Change the bounded behavior.",
        "assumptions": [],
        "file_changes": [
            {
                "path": "src/app.py",
                "change_type": "modify",
                "base_blob_sha": "b" * 40,
                "rationale": "Match the requirement.",
                "unified_diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new",
                "citations": [citation],
            }
        ],
        "test_intents": [],
        "risk_flags": ["behavior_change"],
        "citations": [],
        "model_confidence_band": "medium",
    }


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        (
            "search_repository_paths",
            {"terms": ["service"], "extensions": [".py"], "max_results": 5},
        ),
        (
            "read_repository_files",
            {
                "files": [
                    {
                        "evidence_handle": "E-1234567890abcdef",
                        "repository_path": "src/app.py",
                    }
                ]
            },
        ),
        ("finish", {"proposal": _proposal()}),
        (
            "request_human_review",
            {"reason_code": "insufficient_context", "reason": "More evidence is required."},
        ),
        (
            "refuse",
            {"reason_code": "out_of_scope_request", "reason": "The request is out of scope."},
        ),
    ],
)
def test_five_way_action_union_accepts_exactly_one_action(
    action: str, payload: dict[str, object]
) -> None:
    parsed: ModelAction = TypeAdapter(ModelAction).validate_python({"action": action, **payload})
    assert parsed.action == action


def test_action_union_rejects_unknown_action_and_fields() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ModelAction).validate_python({"action": "shell", "command": "whoami"})
    with pytest.raises(ValidationError):
        TypeAdapter(ModelAction).validate_python(
            {
                "action": "search_repository_paths",
                "terms": ["x"],
                "extensions": [],
                "max_results": 1,
                "command": "forbidden",
            }
        )


def test_finish_proposal_has_no_outcome_field() -> None:
    payload = _proposal()
    payload["outcome"] = "proposed"
    with pytest.raises(ValidationError):
        FinishProposalAction.model_validate({"action": "finish", "proposal": payload})


def test_file_change_modify_and_create_base_sha_rules() -> None:
    modify = _proposal()
    modify["file_changes"][0]["base_blob_sha"] = None  # type: ignore[index]
    with pytest.raises(ValidationError):
        ProposedCodeChange.model_validate(modify)

    create = _proposal()
    create["file_changes"][0]["change_type"] = "create"  # type: ignore[index]
    with pytest.raises(ValidationError):
        ProposedCodeChange.model_validate(create)


def test_step_summary_is_bounded_and_cannot_store_raw_proposal() -> None:
    summary = FinishActionSummary(kind="finish", proposal_size_bytes=128)
    assert summary.model_dump() == {"kind": "finish", "proposal_size_bytes": 128}
    with pytest.raises(ValidationError):
        FinishActionSummary.model_validate(
            {
                "kind": "finish",
                "proposal_size_bytes": 128,
                "proposal": {"unified_diff": "raw-output"},
            }
        )


@pytest.mark.parametrize("value", ["pytest tests", "make test", "x && rm y", "`whoami`"])
def test_test_intent_contains_descriptions_not_commands(value: str) -> None:
    with pytest.raises(ValidationError):
        ProposalTestIntent(
            kind="unit",
            target=value,
            behavior="Describe behavior.",
            expected_result="The behavior is observed.",
        )
