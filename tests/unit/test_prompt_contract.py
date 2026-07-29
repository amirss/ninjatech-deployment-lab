from __future__ import annotations

import pytest

from ninjatech_deployment_lab.code_proposals import prompting as prompting_module
from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.domain import ModelEvidenceBlock
from ninjatech_deployment_lab.code_proposals.prompting import (
    build_model_request,
    build_prompt_contract,
    build_run_scope_key,
    build_source_snapshot_hash,
    model_request_fingerprint,
)
from ninjatech_deployment_lab.code_proposals.scanner import PotentialSecretDetectedError


def test_prompt_contract_is_stable_for_nonsemantic_whitespace() -> None:
    budgets = ContextBudgets()
    first = build_prompt_contract(
        prompt_template_version="v1",
        budgets=budgets,
        system_policy="one   two\n\nthree",
    )
    second = build_prompt_contract(
        prompt_template_version="v1",
        budgets=budgets,
        system_policy=" one two \n three ",
    )
    assert first.prompt_contract_hash == second.prompt_contract_hash


def test_each_material_prompt_contract_change_changes_hash() -> None:
    budgets = ContextBudgets()
    base = build_prompt_contract(prompt_template_version="v1", budgets=budgets)
    variants = (
        build_prompt_contract(
            prompt_template_version="v1", budgets=budgets, system_policy="changed"
        ),
        build_prompt_contract(
            prompt_template_version="v1",
            budgets=budgets,
            repository_tool_contract_version="repository-tools:v2",
        ),
        build_prompt_contract(
            prompt_template_version="v1",
            budgets=budgets,
            evidence_block_format="evidence-blocks:v2",
        ),
        build_prompt_contract(
            prompt_template_version="v1",
            budgets=budgets,
            rendering_rules="canonical-json:utf8:v2",
        ),
        build_prompt_contract(
            prompt_template_version="v1",
            budgets=budgets.model_copy(update={"maximum_model_steps": 9}),
        ),
    )
    assert all(item.prompt_contract_hash != base.prompt_contract_hash for item in variants)


def test_action_and_proposal_schema_changes_change_contract_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = ContextBudgets()
    base = build_prompt_contract(prompt_template_version="v1", budgets=budgets)

    class _ChangedActionAdapter:
        def __init__(self, _: object) -> None:
            pass

        def json_schema(self) -> dict[str, object]:
            return {"type": "changed-action-schema"}

    monkeypatch.setitem(vars(prompting_module), "TypeAdapter", _ChangedActionAdapter)
    changed_action = build_prompt_contract(prompt_template_version="v1", budgets=budgets)
    assert changed_action.prompt_contract_hash != base.prompt_contract_hash
    monkeypatch.undo()

    proposal_model = vars(prompting_module)["ProposedCodeChange"]
    monkeypatch.setattr(
        proposal_model,
        "model_json_schema",
        lambda: {"type": "changed-proposal-schema"},
    )
    changed_proposal = build_prompt_contract(prompt_template_version="v1", budgets=budgets)
    assert changed_proposal.prompt_contract_hash != base.prompt_contract_hash


def test_evidence_order_does_not_change_request_or_source_snapshot() -> None:
    budgets = ContextBudgets()
    contract = build_prompt_contract(prompt_template_version="v1", budgets=budgets)
    evidence = (
        ModelEvidenceBlock(
            evidence_handle="E-bbbbbbbbbbbbbbbb",
            evidence_kind="requirement",
            source_version="v2",
            content_hash="b" * 64,
            untrusted_text="two",
        ),
        ModelEvidenceBlock(
            evidence_handle="E-aaaaaaaaaaaaaaaa",
            evidence_kind="requirement",
            source_version="v1",
            content_hash="a" * 64,
            untrusted_text="one",
        ),
    )
    first = build_model_request(
        contract=contract,
        budgets=budgets,
        logical_step_number=1,
        completed_response_count=0,
        evidence_blocks=evidence,
    )
    second = build_model_request(
        contract=contract,
        budgets=budgets,
        logical_step_number=1,
        completed_response_count=0,
        evidence_blocks=tuple(reversed(evidence)),
    )
    assert model_request_fingerprint(first) == model_request_fingerprint(second)
    semantic = tuple(item.model_dump(mode="json") for item in evidence)
    assert build_source_snapshot_hash(semantic) == build_source_snapshot_hash(
        tuple(reversed(semantic))
    )


def test_run_scope_contains_semantics_but_no_execution_identity() -> None:
    key = build_run_scope_key(
        deployment_scope_id="demo",
        repository_id=123,
        base_commit_sha="a" * 40,
        jira_issue_key="ENG-123",
        jira_source_version="jira-v1",
        canonical_service_id="payments",
        source_snapshot_hash="b" * 64,
        provider="recorded",
        model_name="fixture",
        prompt_template_version="v1",
        prompt_contract_hash="c" * 64,
    )
    assert key.startswith("code_change_proposal:v1:")
    for forbidden in ("task", "attempt", "worker", "lease", "request_id"):
        assert forbidden not in key


def test_prompt_version_and_contract_hash_both_change_run_scope() -> None:
    def scope(*, version: str, contract_hash: str) -> str:
        return build_run_scope_key(
            deployment_scope_id="demo",
            repository_id=123,
            base_commit_sha="a" * 40,
            jira_issue_key="ENG-123",
            jira_source_version="jira-v1",
            canonical_service_id="payments",
            source_snapshot_hash="b" * 64,
            provider="recorded",
            model_name="fixture",
            prompt_template_version=version,
            prompt_contract_hash=contract_hash,
        )

    base = scope(version="v1", contract_hash="c" * 64)
    assert scope(version="v2", contract_hash="c" * 64) != base
    assert scope(version="v1", contract_hash="d" * 64) != base


def test_model_request_schema_excludes_database_and_execution_identifiers() -> None:
    budgets = ContextBudgets()
    contract = build_prompt_contract(prompt_template_version="v1", budgets=budgets)
    block = ModelEvidenceBlock(
        evidence_handle="E-aaaaaaaaaaaaaaaa",
        evidence_kind="requirement",
        source_version="v1",
        content_hash="a" * 64,
        untrusted_text="safe",
    )
    request = build_model_request(
        contract=contract,
        budgets=budgets,
        logical_step_number=1,
        completed_response_count=0,
        evidence_blocks=(block,),
    )
    rendered = request.model_dump_json()
    for forbidden in (
        "source_artifact_id",
        "task_id",
        "attempt_id",
        "worker_id",
        "request_id",
    ):
        assert forbidden not in rendered


def test_secret_scanner_runs_before_evidence_enters_model_request() -> None:
    budgets = ContextBudgets()
    contract = build_prompt_contract(prompt_template_version="v1", budgets=budgets)
    block = ModelEvidenceBlock(
        evidence_handle="E-aaaaaaaaaaaaaaaa",
        evidence_kind="repository_file",
        source_version="b" * 40,
        repository_path="src/app.py",
        blob_sha="b" * 40,
        content_hash="a" * 64,
        untrusted_text="ghp_" + "a" * 36,
    )
    with pytest.raises(PotentialSecretDetectedError):
        build_model_request(
            contract=contract,
            budgets=budgets,
            logical_step_number=1,
            completed_response_count=0,
            evidence_blocks=(block,),
        )
