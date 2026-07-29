from __future__ import annotations

import logging

import pytest

from ninjatech_deployment_lab.code_proposals.model import AgentRun, AgentStep
from ninjatech_deployment_lab.code_proposals.scanner import (
    ModelEgressScanner,
    PotentialSecretDetectedError,
    SecretKind,
)
from ninjatech_deployment_lab.observability import JsonFormatter


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        ("-----BEGIN PRIVATE KEY-----", SecretKind.PEM_PRIVATE_KEY),
        ("ghp_" + "a" * 36, SecretKind.GITHUB_TOKEN),
        ("xoxb-" + "1" * 30, SecretKind.SLACK_TOKEN),
        ("sk-proj-" + "a" * 30, SecretKind.OPENAI_KEY),
        ("AKIA" + "A" * 16, SecretKind.AWS_ACCESS_KEY),
        ("Authorization: Bearer " + "x" * 20, SecretKind.AUTHORIZATION_LITERAL),
        ('password = "' + "x" * 20 + '"', SecretKind.PASSWORD_ASSIGNMENT),
        ("private_token=" + "x" * 20, SecretKind.PASSWORD_ASSIGNMENT),
    ],
)
def test_high_confidence_secret_patterns_block_egress(secret: str, kind: SecretKind) -> None:
    scanner = ModelEgressScanner()
    assert kind in {item.kind for item in scanner.scan(secret)}
    with pytest.raises(PotentialSecretDetectedError) as caught:
        scanner.require_safe(secret)
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "safe",
    [
        "password validation rejects empty values",
        "Bearer tokens are supplied by the credential provider.",
        "sk-example-placeholder",
        "AWS account identifiers are not access keys.",
    ],
)
def test_common_non_secret_text_is_not_flagged(safe: str) -> None:
    assert ModelEgressScanner().scan(safe) == ()


def test_logs_allow_safe_run_identifiers_but_not_prompt_source_diff_or_secret() -> None:
    record = logging.LogRecord(
        "agent.test", logging.INFO, __file__, 1, "agent_step_recorded", (), None
    )
    record.agent_run_id = "run-safe"
    record.prompt = "prompt-secret"
    record.source = "source-secret"
    record.unified_diff = "diff-secret"
    record.authorization = "Bearer credential-secret"
    rendered = JsonFormatter("test").format(record)
    assert "run-safe" in rendered
    for forbidden in ("prompt-secret", "source-secret", "diff-secret", "credential-secret"):
        assert forbidden not in rendered


def test_agent_tables_have_no_raw_prompt_source_response_or_unvalidated_diff_columns() -> None:
    columns = {column.name for column in AgentRun.__table__.columns} | {
        column.name for column in AgentStep.__table__.columns
    }
    assert {
        "raw_prompt",
        "prompt",
        "source_text",
        "raw_response",
        "model_output",
        "unified_diff",
        "credential",
        "authorization",
    }.isdisjoint(columns)
