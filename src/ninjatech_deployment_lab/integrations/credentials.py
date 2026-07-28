from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import SecretStr


class CredentialProvider(Protocol):
    """Connector-only credential boundary."""

    def get_secret(self) -> str | None: ...


class EnvironmentOrFileCredential:
    """Resolve one secret without exposing it in representations or logs."""

    def __init__(self, *, value: SecretStr | None, path: Path | None) -> None:
        self._value = value
        self._path = path

    def get_secret(self) -> str | None:
        if self._value is not None:
            return self._value.get_secret_value()
        if self._path is None:
            return None
        secret = self._path.read_text(encoding="utf-8").strip()
        if not secret:
            raise ValueError("credential file is empty")
        return secret
