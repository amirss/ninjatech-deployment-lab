from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application configuration loaded from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="NINJATECH_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "NinjaTech Deployment Lab"
    environment: Literal["development", "test", "demo", "sandbox", "staging", "production"] = (
        "development"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: str = Field(min_length=1)
    db_ready_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=60)
    worker_lease_duration_seconds: float = Field(default=30.0, ge=1, le=3600)
    worker_heartbeat_interval_seconds: float = Field(default=10.0, ge=0.1, le=1200)
    worker_handler_timeout_seconds: float = Field(default=300.0, ge=0.1, le=86400)
    worker_shutdown_grace_seconds: float = Field(default=20.0, ge=0.1, le=3600)
    worker_default_max_attempts: int = Field(default=3, ge=1, le=100)
    worker_retry_base_seconds: float = Field(default=2.0, ge=0.01, le=3600)
    worker_retry_cap_seconds: float = Field(default=300.0, ge=0.01, le=86400)
    worker_max_result_bytes: int = Field(default=262144, ge=1, le=16777216)
    enable_diagnostic_handler: bool = False
    enable_deployment_context_sync: bool = False
    deployment_scope_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    deployment_allowed_service_ids: tuple[str, ...] = ()
    deployment_allowed_github_repositories: tuple[str, ...] = ()
    deployment_allowed_jira_projects: tuple[str, ...] = ()
    deployment_minimum_policy_version: int = Field(default=1, ge=1)
    service_catalog_base_url: str = "http://127.0.0.1:8090/catalog"
    jira_base_url: str = "http://127.0.0.1:8090/jira"
    github_base_url: str = "http://127.0.0.1:8090/github"
    github_expected_login: str | None = None
    service_catalog_token: SecretStr | None = None
    service_catalog_token_file: Path | None = None
    jira_api_token: SecretStr | None = None
    jira_api_token_file: Path | None = None
    jira_username: str | None = Field(default=None, min_length=1, max_length=255)
    github_token: SecretStr | None = None
    github_token_file: Path | None = None
    integration_http_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    integration_http_read_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    integration_http_write_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    integration_http_pool_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    integration_provider_write_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    integration_settlement_delay_seconds: float = Field(default=3.0, ge=0.1, le=300)
    integration_max_response_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    integration_max_pages: int = Field(default=5, ge=1, le=20)
    integration_max_items: int = Field(default=200, ge=1, le=1000)
    integration_max_retry_after_seconds: float = Field(default=60.0, ge=0, le=3600)
    source_artifact_max_bytes: int = Field(default=262144, ge=1024, le=16777216)

    @field_validator("database_url")
    @classmethod
    def require_async_postgresql_url(cls, value: str) -> str:
        """Reject database URLs that bypass the selected PostgreSQL async driver."""
        if not value.startswith("postgresql+asyncpg://"):
            msg = "database_url must use the postgresql+asyncpg scheme"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_worker_safety(self) -> Settings:
        """Reject unsafe lease timing and accidental production diagnostics."""
        if self.worker_heartbeat_interval_seconds * 3 > self.worker_lease_duration_seconds:
            msg = "worker heartbeat interval must be at most one third of the lease duration"
            raise ValueError(msg)
        if self.worker_retry_base_seconds > self.worker_retry_cap_seconds:
            msg = "worker retry base must not exceed the retry cap"
            raise ValueError(msg)
        if self.enable_diagnostic_handler and self.environment not in {"development", "test"}:
            msg = "diagnostic handler may only be enabled in development or test"
            raise ValueError(msg)
        if self.enable_deployment_context_sync:
            if self.environment not in {"development", "test", "demo", "sandbox"}:
                msg = (
                    "deployment_context_sync cannot be enabled in staging or production "
                    "without authentication and tenancy"
                )
                raise ValueError(msg)
            if self.deployment_scope_id is None:
                msg = "deployment_scope_id is required when deployment_context_sync is enabled"
                raise ValueError(msg)
            if not self.deployment_allowed_service_ids:
                msg = "at least one deployment service must be allowlisted"
                raise ValueError(msg)
            if not self.deployment_allowed_github_repositories:
                msg = "at least one GitHub repository must be allowlisted"
                raise ValueError(msg)
            if not self.deployment_allowed_jira_projects:
                msg = "at least one Jira project must be allowlisted"
                raise ValueError(msg)
            for base_url in (
                self.service_catalog_base_url,
                self.jira_base_url,
                self.github_base_url,
            ):
                self._validate_integration_base_url(base_url)
            if (
                self.integration_provider_write_timeout_seconds
                >= self.worker_lease_duration_seconds
            ):
                msg = "provider write timeout must be shorter than the worker lease"
                raise ValueError(msg)
        for inline_secret, secret_file, name in (
            (self.service_catalog_token, self.service_catalog_token_file, "service catalog"),
            (self.jira_api_token, self.jira_api_token_file, "Jira"),
            (self.github_token, self.github_token_file, "GitHub"),
        ):
            if inline_secret is not None and secret_file is not None:
                msg = f"{name} credential must use either an environment value or a file"
                raise ValueError(msg)
        return self

    def _validate_integration_base_url(self, value: str) -> None:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            msg = "integration base URLs must be absolute HTTP(S) URLs"
            raise ValueError(msg)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            msg = "integration base URLs must not contain credentials, queries, or fragments"
            raise ValueError(msg)
        if self.environment in {"staging", "production"} and parsed.scheme != "https":
            msg = "staging and production integration base URLs must use HTTPS"
            raise ValueError(msg)


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""
    return Settings()
