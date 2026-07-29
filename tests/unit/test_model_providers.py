from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.domain import ModelEvidenceBlock, ModelRequest
from ninjatech_deployment_lab.code_proposals.prompting import (
    build_model_request,
    build_prompt_contract,
    model_request_fingerprint,
)
from ninjatech_deployment_lab.code_proposals.providers import ModelProviderContractError
from ninjatech_deployment_lab.code_proposals.recorded_provider import (
    RecordedExchange,
    RecordedFixtureSet,
    RecordedModelProvider,
    load_recorded_fixture_set,
)


def _request(completed: int = 0) -> ModelRequest:
    budgets = ContextBudgets()
    return build_model_request(
        contract=build_prompt_contract(prompt_template_version="v1", budgets=budgets),
        budgets=budgets,
        logical_step_number=completed + 1,
        completed_response_count=completed,
        evidence_blocks=(
            ModelEvidenceBlock(
                evidence_handle="E-1234567890abcdef",
                evidence_kind="requirement",
                source_version="jira-v1",
                content_hash="a" * 64,
                untrusted_text="Synthetic requirement.",
            ),
        ),
    )


def _exchange(request: ModelRequest, reason: str) -> RecordedExchange:
    return RecordedExchange(
        expected_request_fingerprint=model_request_fingerprint(request),
        response={
            "action": "refuse",
            "reason_code": "out_of_scope_request",
            "reason": reason,
            "citations": [],
        },
    )


def test_recorded_provider_sequence_uses_completed_response_count_not_process_state() -> None:
    first_request = _request(0)
    second_request = _request(1)
    fixture = RecordedFixtureSet(
        name="ci",
        model_name="recorded-v1",
        exchanges=(
            _exchange(first_request, "first response"),
            _exchange(second_request, "second response"),
        ),
    )
    first_process = RecordedModelProvider(fixture_set=fixture)
    restarted_process = RecordedModelProvider(fixture_set=fixture)

    first = asyncio.run(first_process.complete(first_request))
    replayed = asyncio.run(restarted_process.complete(first_request))
    second = asyncio.run(restarted_process.complete(second_request))

    assert first == replayed
    assert second.action.reason == "second response"  # type: ignore[union-attr]


def test_started_call_without_recorded_response_replays_same_fixture_index() -> None:
    request = _request(0)
    provider = RecordedModelProvider(
        fixture_set=RecordedFixtureSet(
            name="ci",
            model_name="recorded-v1",
            exchanges=(_exchange(request, "same logical response"),),
        )
    )
    assert asyncio.run(provider.complete(request)) == asyncio.run(provider.complete(request))


def test_recorded_provider_rejects_fingerprint_mismatch_and_malformed_response() -> None:
    request = _request(0)
    mismatched = RecordedModelProvider(
        fixture_set=RecordedFixtureSet(
            name="ci",
            model_name="recorded-v1",
            exchanges=(RecordedExchange("0" * 64, {}),),
        )
    )
    with pytest.raises(ModelProviderContractError, match="fingerprint"):
        asyncio.run(mismatched.complete(request))
    malformed = RecordedModelProvider(
        fixture_set=RecordedFixtureSet(
            name="ci",
            model_name="recorded-v1",
            exchanges=(RecordedExchange(model_request_fingerprint(request), {"action": "shell"}),),
        )
    )
    with pytest.raises(ModelProviderContractError, match="contract"):
        asyncio.run(malformed.complete(request))


def test_fixture_loader_accepts_only_bounded_named_repository_resources(
    tmp_path: Path,
) -> None:
    request = _request(0)
    payload = {
        "name": "ci-v1",
        "model_name": "recorded-v1",
        "exchanges": [
            {
                "expected_request_fingerprint": model_request_fingerprint(request),
                "response": {
                    "action": "refuse",
                    "reason_code": "out_of_scope_request",
                    "reason": "bounded",
                    "citations": [],
                },
            }
        ],
    }
    (tmp_path / "ci-v1.json").write_text(json.dumps(payload), encoding="utf-8")
    assert load_recorded_fixture_set("ci-v1", trusted_fixture_directory=tmp_path).name == "ci-v1"
    with pytest.raises(ValueError):
        load_recorded_fixture_set("../outside", trusted_fixture_directory=tmp_path)


def test_version_controlled_fixture_is_synthetic_and_loadable() -> None:
    fixture = load_recorded_fixture_set(
        "refuse-v1",
        trusted_fixture_directory=Path(__file__).parents[1] / "fixtures" / "model",
    )
    assert fixture.model_name == "recorded-contract-v1"
    assert fixture.exchanges[0].input_tokens == 0
