from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    environment: Literal["development", "test", "staging", "production"] = "development"
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
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""
    return Settings()
