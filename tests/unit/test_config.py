from __future__ import annotations

import pytest
from pydantic import ValidationError

from ninjatech_deployment_lab.config import Settings


def test_settings_load_from_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "NINJATECH_DATABASE_URL",
        "postgresql+asyncpg://user:password@localhost:5432/example",
    )
    monkeypatch.setenv("NINJATECH_ENVIRONMENT", "staging")
    monkeypatch.setenv("NINJATECH_DB_READY_TIMEOUT_SECONDS", "4.5")

    settings = Settings()

    assert settings.environment == "staging"
    assert settings.db_ready_timeout_seconds == 4.5


def test_settings_reject_non_async_postgresql_url() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg"):
        Settings(database_url="sqlite+aiosqlite:///local.db")


def test_settings_reject_heartbeat_too_close_to_lease() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://localhost/test",
            worker_lease_duration_seconds=30,
            worker_heartbeat_interval_seconds=11,
        )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_diagnostic_handler_cannot_be_enabled_outside_dev_or_test(
    environment: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "database_url": "postgresql+asyncpg://localhost/test",
                "environment": environment,
                "enable_diagnostic_handler": True,
            }
        )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployment_workflow_cannot_be_enabled_without_authentication(
    environment: str,
) -> None:
    with pytest.raises(
        ValidationError,
        match="cannot be enabled in staging or production",
    ):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/database",
            environment=environment,
            enable_deployment_context_sync=True,
            deployment_scope_id="unsafe",
            deployment_allowed_service_ids=("service",),
            deployment_allowed_github_repositories=("owner/repository",),
            deployment_allowed_jira_projects=("ENG",),
        )


def test_deployment_workflow_requires_trusted_scope_configuration() -> None:
    with pytest.raises(ValidationError, match="deployment_scope_id is required"):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/database",
            environment="test",
            enable_deployment_context_sync=True,
        )
