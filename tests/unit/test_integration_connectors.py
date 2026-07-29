from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.connectors import (
    GitHubClient,
    _normalize_comment,
)
from ninjatech_deployment_lab.integrations.credentials import CredentialProvider
from ninjatech_deployment_lab.integrations.http import IntegrationHttpClient, JsonHttpResponse
from ninjatech_deployment_lab.tasks.schemas import JsonValue


class _NoCredential:
    def get_secret(self) -> str | None:
        return None


class _SequenceHttp:
    def __init__(self, payloads: list[JsonValue]) -> None:
        self._payloads = payloads

    async def request_json(self, **kwargs: Any) -> JsonHttpResponse:
        del kwargs
        return JsonHttpResponse(
            status_code=200,
            headers=cast(Mapping[str, str], {}),
            payload=self._payloads.pop(0),
        )


def _settings(*, expected_login: str = "simulator-bot") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        environment="test",
        github_expected_login=expected_login,
    )


def _client(payloads: list[JsonValue], *, expected_login: str = "simulator-bot") -> GitHubClient:
    return GitHubClient(
        http=cast(IntegrationHttpClient, _SequenceHttp(payloads)),
        base_url="https://github.example/api",
        credential=cast(CredentialProvider, _NoCredential()),
        settings=_settings(expected_login=expected_login),
    )


def test_github_issue_normalization_identifies_pull_requests() -> None:
    client = _client(
        [
            {
                "full_name": "customer/example-service",
                "id": 424242,
                "visibility": "private",
                "archived": False,
                "default_branch": "main",
            },
            {"sha": "a" * 40},
            {
                "number": 42,
                "state": "open",
                "title": "Deployment context",
                "html_url": "https://github.example/customer/example-service/pull/42",
                "updated_at": "2026-07-28T12:00:00Z",
                "pull_request": {
                    "url": "https://github.example/api/repos/customer/example-service/pulls/42"
                },
            },
        ]
    )

    context = asyncio.run(
        client.fetch_context("customer/example-service", 42, correlation_id="test")
    )

    assert context.is_pull_request is True


def test_github_identity_comparison_is_case_insensitive() -> None:
    client = _client([{"login": "Simulator-Bot"}], expected_login="simulator-bot")

    assert asyncio.run(client.verify_identity(correlation_id="test")) is True


def test_github_comment_normalization_preserves_exact_safe_anchor() -> None:
    comment = _normalize_comment(
        {
            "id": 123,
            "body": "bounded",
            "html_url": (
                "https://github.example/customer/example-service/issues/42"
                "?temporary=secret#issuecomment-123"
            ),
            "updated_at": "2026-07-29T12:00:00Z",
        },
        ambiguous_on_failure=False,
    )
    assert comment.url == (
        "https://github.example/customer/example-service/issues/42#issuecomment-123"
    )
