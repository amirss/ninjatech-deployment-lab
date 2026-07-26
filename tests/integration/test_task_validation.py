from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.main import create_app

VALID_PAYLOAD = {
    "task_type": "code_change",
    "input": {
        "repository": "example/repository",
        "issue_number": 123,
    },
}


@pytest.mark.parametrize(
    "invalid_key",
    [
        "",
        "a" * 256,
        "contains space",
    ],
)
def test_api_rejects_invalid_idempotency_key(invalid_key: str) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@127.0.0.1:5432/test",
        environment="test",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/tasks",
            headers={"Idempotency-Key": invalid_key},
            json=VALID_PAYLOAD,
        )

    assert response.status_code == 422


def test_api_requires_idempotency_key() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@127.0.0.1:5432/test",
        environment="test",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/tasks", json=VALID_PAYLOAD)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"task_type": "code_change", "input": ["not", "an", "object"]},
        {"task_type": "CODE_CHANGE", "input": {}},
        {"task_type": "code-change", "input": {}},
        {"task_type": "code_change", "input": {}, "unexpected": True},
    ],
)
def test_api_rejects_invalid_task_payload(invalid_payload: object) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@127.0.0.1:5432/test",
        environment="test",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/tasks",
            headers={"Idempotency-Key": "valid-validation-key"},
            json=invalid_payload,
        )

    assert response.status_code == 422
    assert all("input" not in error for error in response.json()["detail"])


@pytest.mark.parametrize("non_finite_literal", ["NaN", "Infinity", "-Infinity"])
def test_api_rejects_recursive_non_finite_number(non_finite_literal: str) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@127.0.0.1:5432/test",
        environment="test",
    )
    request_body = (
        f'{{"task_type":"code_change","input":{{"nested":{{"values":[{non_finite_literal}]}}}}}}'
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/tasks",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "valid-non-finite-key",
            },
            content=request_body,
        )

    assert response.status_code == 422
    assert non_finite_literal not in response.text


def test_validation_logs_exclude_raw_key_and_complete_task_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_key = "raw-validation-key-never-log"
    sensitive_value = "complete-task-input-never-log"
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@127.0.0.1:5432/test",
        environment="test",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/tasks",
            headers={"Idempotency-Key": raw_key},
            json={
                "task_type": "INVALID",
                "input": {"sensitive": sensitive_value},
            },
        )

    captured = capsys.readouterr().out
    assert response.status_code == 422
    assert raw_key not in captured
    assert sensitive_value not in captured
