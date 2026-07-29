from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class SecretKind(StrEnum):
    PEM_PRIVATE_KEY = "pem_private_key"
    GITHUB_TOKEN = "github_token"
    SLACK_TOKEN = "slack_token"
    OPENAI_KEY = "openai_key"
    AWS_ACCESS_KEY = "aws_access_key"
    AUTHORIZATION_LITERAL = "authorization_literal"
    PASSWORD_ASSIGNMENT = "password_assignment"


@dataclass(frozen=True, slots=True)
class SecretDetection:
    kind: SecretKind


_PATTERNS: tuple[tuple[SecretKind, re.Pattern[str]], ...] = (
    (
        SecretKind.PEM_PRIVATE_KEY,
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        SecretKind.GITHUB_TOKEN,
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}\b"),
    ),
    (
        SecretKind.SLACK_TOKEN,
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,255}\b"),
    ),
    (
        SecretKind.OPENAI_KEY,
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,255}\b"),
    ),
    (
        SecretKind.AWS_ACCESS_KEY,
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        SecretKind.AUTHORIZATION_LITERAL,
        re.compile(
            r"(?im)^\s*authorization\s*[:=]\s*[\"']?(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"
        ),
    ),
    (
        SecretKind.PASSWORD_ASSIGNMENT,
        re.compile(
            r"(?im)^\s*(?:password|passwd|private_token|api_token)\s*[:=]\s*"
            r"(?:[\"'][^\"'\s]{12,}[\"']|[A-Za-z0-9._~+/=-]{16,})"
        ),
    ),
)


class ModelEgressScanner:
    """High-confidence deterministic scanner; intentionally not a full DLP product."""

    def scan(self, text: str) -> tuple[SecretDetection, ...]:
        return tuple(
            SecretDetection(kind) for kind, pattern in _PATTERNS if pattern.search(text) is not None
        )

    def require_safe(self, text: str) -> None:
        if self.scan(text):
            raise PotentialSecretDetectedError("potential_secret_detected")


class PotentialSecretDetectedError(ValueError):
    pass
