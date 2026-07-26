from __future__ import annotations

from fastapi.testclient import TestClient

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.main import create_app


def test_health_uses_full_http_stack_and_preserves_request_id() -> None:
    settings = Settings(
        database_url=("postgresql+asyncpg://ninjatech:test-only@127.0.0.1:5432/ninjatech_test"),
        environment="test",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health", headers={"X-Request-ID": "integration-test-123"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "integration-test-123"
