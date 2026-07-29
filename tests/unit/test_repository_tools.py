from __future__ import annotations

import pytest

from ninjatech_deployment_lab.code_proposals.context import (
    BudgetExceededError,
    BudgetLedger,
    ContextBudgets,
)
from ninjatech_deployment_lab.code_proposals.domain import SearchPathsAction
from ninjatech_deployment_lab.code_proposals.repository import ManifestEntry, RepositoryManifest
from ninjatech_deployment_lab.code_proposals.tools import search_repository_paths


def _manifest() -> RepositoryManifest:
    return RepositoryManifest(
        repository_id=1,
        commit_sha="a" * 40,
        complete=True,
        entries=(
            ManifestEntry(
                repository_id=1,
                commit_sha="a" * 40,
                path="src/payments/service.py",
                object_type="blob",
                mode="100644",
                blob_sha="b" * 40,
                byte_size=100,
                text_eligible=True,
            ),
        ),
    )


def test_search_is_manifest_only_deterministic_and_budgeted() -> None:
    ledger = BudgetLedger(ContextBudgets(maximum_repository_tool_calls=1))
    action = SearchPathsAction(
        action="search_repository_paths",
        terms=("payment",),
        extensions=(".py",),
        max_results=5,
    )
    assert search_repository_paths(_manifest(), action, ledger=ledger)[0].path.endswith(
        "service.py"
    )
    with pytest.raises(BudgetExceededError):
        search_repository_paths(_manifest(), action, ledger=ledger)


def test_repeated_blob_does_not_increase_distinct_or_total_bytes() -> None:
    ledger = BudgetLedger(ContextBudgets())
    assert ledger.record_blob("b" * 40, 100)
    assert not ledger.record_blob("b" * 40, 100)
    assert ledger.distinct_files == 1
    assert ledger.total_source_bytes == 100


def test_issue_and_file_read_limits_are_enforced_in_code() -> None:
    ledger = BudgetLedger(
        ContextBudgets(
            maximum_issue_description_bytes=10,
            maximum_files_per_read=2,
        )
    )
    ledger.record_issue_description(10)
    ledger.record_file_read_call(2)
    with pytest.raises(BudgetExceededError):
        ledger.record_issue_description(11)
    with pytest.raises(BudgetExceededError):
        ledger.record_file_read_call(3)
