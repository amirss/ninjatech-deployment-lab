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


@pytest.mark.parametrize("expected_login", [None, "   "])
def test_deployment_workflow_requires_expected_github_principal(
    expected_login: str | None,
) -> None:
    with pytest.raises(ValidationError, match="github_expected_login is required"):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/database",
            environment="test",
            enable_deployment_context_sync=True,
            deployment_scope_id="controlled-sandbox",
            deployment_allowed_service_ids=("service",),
            deployment_allowed_github_repositories=("owner/repository",),
            deployment_allowed_jira_projects=("ENG",),
            github_expected_login=expected_login,
        )


def test_recorded_proposal_foundation_needs_no_credential_or_egress_flag() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/database",
        environment="test",
        enable_code_change_proposal=True,
        model_provider="recorded",
    )
    assert settings.openai_api_key is None
    assert not settings.enable_model_data_egress
    assert settings.code_proposal_budgets().maximum_model_steps == 8


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_proposal_feature_remains_fail_closed_without_authentication(
    environment: str,
) -> None:
    with pytest.raises(ValidationError, match="cannot be enabled"):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/database",
            environment=environment,
            enable_code_change_proposal=True,
        )


def test_openai_provider_is_forbidden_in_test_and_ci() -> None:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://user:pass@localhost/database",
        "enable_code_change_proposal": True,
        "model_provider": "openai",
        "model_name": "trusted-model",
        "enable_model_data_egress": True,
        "run_model_sandbox": True,
        "openai_api_key": "test-only-placeholder",
    }
    with pytest.raises(ValidationError, match="ordinary test"):
        Settings.model_validate({**base, "environment": "test"})
    with pytest.raises(ValidationError, match="CI=true"):
        Settings.model_validate({**base, "environment": "sandbox", "CI": True})


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.openai.com/v1",
        "https://example.com/v1",
        "https://user:password@api.openai.com/v1",
        "https://api.openai.com/v1?query=unsafe",
    ],
)
def test_future_openai_provider_accepts_only_official_https_host(base_url: str) -> None:
    with pytest.raises(ValidationError, match="official HTTPS"):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/database",
            environment="sandbox",
            enable_code_change_proposal=True,
            model_provider="openai",
            model_name="trusted-model",
            model_base_url=base_url,
            enable_model_data_egress=True,
            run_model_sandbox=True,
            openai_api_key="test-only-placeholder",
        )
