from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.main import create_app


def test_ready_fails_closed_when_postgresql_is_unavailable() -> None:
    settings = Settings(
        database_url=("postgresql+asyncpg://ninjatech:test-only@127.0.0.1:5432/ninjatech_test"),
        environment="test",
    )
    application = create_app(settings)
    unavailable_engine = MagicMock(spec=AsyncEngine)
    unavailable_engine.connect.side_effect = RuntimeError("database is unavailable")
    application.state.database_engine = unavailable_engine

    with TestClient(application) as client:
        response = client.get("/ready", headers={"X-Request-ID": "readiness-failure-123"})

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert response.headers["X-Request-ID"] == "readiness-failure-123"


@pytest.mark.postgres
def test_ready_with_live_postgresql() -> None:
    database_url = os.getenv("NINJATECH_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("NINJATECH_TEST_DATABASE_URL is not configured")

    settings = Settings(database_url=database_url, environment="test")

    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["X-Request-ID"]
