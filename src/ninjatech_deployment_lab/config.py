from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from ninjatech_deployment_lab.code_proposals.context import ContextBudgets
from ninjatech_deployment_lab.code_proposals.domain import ModelProviderName

SlackChannelId = Annotated[str, StringConstraints(pattern=r"^[CG][A-Z0-9]{8,30}$")]


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
    enable_slack_notification: bool = False
    slack_base_url: str = "http://127.0.0.1:8090/slack"
    slack_bot_token: SecretStr | None = None
    slack_bot_token_file: Path | None = None
    slack_expected_team_id: str | None = Field(default=None, min_length=1, max_length=255)
    slack_expected_user_id: str | None = Field(default=None, min_length=1, max_length=255)
    slack_expected_bot_id: str | None = Field(default=None, min_length=1, max_length=255)
    deployment_allowed_slack_channels: tuple[SlackChannelId, ...] = ()
    slack_max_text_chars: int = Field(default=1000, ge=100, le=4000)
    slack_write_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
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
    enable_code_change_proposal: bool = False
    enable_model_data_egress: bool = False
    run_model_sandbox: bool = False
    model_provider: ModelProviderName = ModelProviderName.RECORDED
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    model_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr | None = None
    openai_api_key_file: Path | None = None
    model_prompt_template_version: str = Field(
        default="code-change-proposal-v1",
        pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$",
    )
    recorded_model_fixture_set: str = Field(
        default="ci-v1",
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$",
    )
    model_minimum_policy_version: int = Field(default=1, ge=1)
    model_maximum_manifest_entries: int = Field(default=2000, ge=1, le=10000)
    model_maximum_manifest_bytes: int = Field(default=262144, ge=1024, le=4194304)
    model_maximum_steps: int = Field(default=8, ge=1, le=50)
    model_maximum_calls: int = Field(default=8, ge=1, le=50)
    model_maximum_repository_tool_calls: int = Field(default=6, ge=1, le=50)
    model_maximum_files_per_read: int = Field(default=5, ge=1, le=20)
    model_maximum_distinct_files: int = Field(default=16, ge=1, le=100)
    model_maximum_bytes_per_file: int = Field(default=65536, ge=1, le=1048576)
    model_maximum_total_source_bytes: int = Field(default=393216, ge=1024, le=8388608)
    model_maximum_issue_description_bytes: int = Field(default=32768, ge=1, le=1048576)
    model_maximum_prompt_bytes: int = Field(default=524288, ge=1024, le=8388608)
    model_maximum_output_tokens: int = Field(default=8192, ge=1, le=65536)
    model_maximum_output_bytes: int = Field(default=262144, ge=1024, le=1048576)
    model_maximum_proposal_bytes: int = Field(default=131072, ge=1024, le=1048576)
    model_maximum_changed_files: int = Field(default=8, ge=1, le=50)
    model_maximum_diff_bytes: int = Field(default=65536, ge=1, le=1048576)
    ci: bool = Field(default=False, validation_alias=AliasChoices("CI", "NINJATECH_CI"))

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
            if self.github_expected_login is None or not self.github_expected_login.strip():
                msg = "github_expected_login is required when deployment_context_sync is enabled"
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
        if self.enable_slack_notification:
            if self.environment not in {"development", "test", "demo", "sandbox"}:
                msg = (
                    "Slack notifications cannot be enabled in staging or production "
                    "without authentication and tenancy"
                )
                raise ValueError(msg)
            if not self.enable_deployment_context_sync:
                msg = "Slack notifications require deployment_context_sync"
                raise ValueError(msg)
            if self.slack_expected_team_id is None or not self.slack_expected_team_id.strip():
                msg = "slack_expected_team_id is required when Slack is enabled"
                raise ValueError(msg)
            if self.slack_expected_user_id is None or not self.slack_expected_user_id.strip():
                msg = "slack_expected_user_id is required when Slack is enabled"
                raise ValueError(msg)
            if not self.deployment_allowed_slack_channels:
                msg = "at least one Slack channel must be allowlisted"
                raise ValueError(msg)
            if self.slack_bot_token is None and self.slack_bot_token_file is None:
                msg = "Slack requires one bot-token credential source"
                raise ValueError(msg)
            if (
                self.slack_bot_token_file is not None
                and not self.slack_bot_token_file.is_absolute()
            ):
                msg = "Slack bot-token file path must be absolute"
                raise ValueError(msg)
            self._validate_integration_base_url(self.slack_base_url)
            if self.slack_write_timeout_seconds >= self.worker_lease_duration_seconds:
                msg = "Slack write timeout must be shorter than the worker lease"
                raise ValueError(msg)
        if self.enable_code_change_proposal:
            if self.environment in {"staging", "production"}:
                msg = (
                    "code_change_proposal cannot be enabled in staging or production "
                    "without authentication, tenancy, and production data controls"
                )
                raise ValueError(msg)
            if self.model_provider is ModelProviderName.OPENAI:
                if self.environment not in {"development", "demo", "sandbox"}:
                    raise ValueError("OpenAI model provider is forbidden in ordinary test")
                if self.ci or os.getenv("CI", "").casefold() == "true":
                    raise ValueError("OpenAI model provider is forbidden when CI=true")
                if not self.enable_model_data_egress or not self.run_model_sandbox:
                    raise ValueError(
                        "OpenAI model provider requires explicit egress and sandbox flags"
                    )
                if self.model_name is None or not self.model_name.strip():
                    raise ValueError("model_name is required for OpenAI provider")
                if self.openai_api_key is None and self.openai_api_key_file is None:
                    raise ValueError("OpenAI provider requires one credential source")
                if (
                    self.openai_api_key_file is not None
                    and not self.openai_api_key_file.is_absolute()
                ):
                    raise ValueError("OpenAI API-key file path must be absolute")
                parsed_model_url = urlsplit(self.model_base_url)
                if (
                    parsed_model_url.scheme != "https"
                    or parsed_model_url.hostname != "api.openai.com"
                    or parsed_model_url.username is not None
                    or parsed_model_url.password is not None
                    or parsed_model_url.query
                    or parsed_model_url.fragment
                ):
                    raise ValueError("OpenAI base URL must use the official HTTPS host")
        for inline_secret, secret_file, name in (
            (self.service_catalog_token, self.service_catalog_token_file, "service catalog"),
            (self.jira_api_token, self.jira_api_token_file, "Jira"),
            (self.github_token, self.github_token_file, "GitHub"),
            (self.slack_bot_token, self.slack_bot_token_file, "Slack"),
            (self.openai_api_key, self.openai_api_key_file, "OpenAI"),
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

    def code_proposal_budgets(self) -> ContextBudgets:
        """Return the code-enforced application ceiling for future proposal runs."""
        return ContextBudgets(
            maximum_manifest_entries=self.model_maximum_manifest_entries,
            maximum_manifest_bytes=self.model_maximum_manifest_bytes,
            maximum_model_steps=self.model_maximum_steps,
            maximum_model_calls=self.model_maximum_calls,
            maximum_repository_tool_calls=self.model_maximum_repository_tool_calls,
            maximum_files_per_read=self.model_maximum_files_per_read,
            maximum_distinct_files=self.model_maximum_distinct_files,
            maximum_bytes_per_file=self.model_maximum_bytes_per_file,
            maximum_total_source_bytes=self.model_maximum_total_source_bytes,
            maximum_issue_description_bytes=self.model_maximum_issue_description_bytes,
            maximum_prompt_bytes=self.model_maximum_prompt_bytes,
            maximum_output_tokens=self.model_maximum_output_tokens,
            maximum_output_bytes=self.model_maximum_output_bytes,
            maximum_proposal_bytes=self.model_maximum_proposal_bytes,
            maximum_changed_files=self.model_maximum_changed_files,
            maximum_diff_bytes=self.model_maximum_diff_bytes,
        )


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""
    return Settings()
