from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.connectors import GitHubClient, JiraClient
from ninjatech_deployment_lab.integrations.credentials import EnvironmentOrFileCredential
from ninjatech_deployment_lab.integrations.http import IntegrationHttpClient
from ninjatech_deployment_lab.integrations.slack import SlackClient, SlackMessageRequest

pytestmark = pytest.mark.sandbox


def _require_sandbox(*, writes: bool = False) -> Settings:
    if os.getenv("NINJATECH_RUN_SANDBOX_TESTS", "").casefold() != "true":
        pytest.skip("NINJATECH_RUN_SANDBOX_TESTS is not true")
    if writes and os.getenv("NINJATECH_RUN_SANDBOX_WRITES", "").casefold() != "true":
        pytest.skip("NINJATECH_RUN_SANDBOX_WRITES is not true")
    if os.getenv("NINJATECH_SANDBOX_OWNER_CONFIRMATION") != "amirss":
        pytest.skip("Amir-controlled sandbox ownership was not explicitly confirmed")
    settings = Settings()
    if not settings.enable_deployment_context_sync or not settings.enable_slack_notification:
        pytest.skip("deployment and Slack sandbox features are not enabled")
    if not settings.deployment_allowed_github_repositories or any(
        not repository.casefold().startswith("amirss/")
        for repository in settings.deployment_allowed_github_repositories
    ):
        pytest.skip("sandbox GitHub allowlist is not restricted to amirss repositories")
    return settings


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        pytest.skip(f"{name} is not configured")
    return value.strip()


def test_real_provider_identities_and_read_contracts() -> None:
    settings = _require_sandbox()
    jira_issue_key = _required_environment("NINJATECH_SANDBOX_JIRA_ISSUE_KEY")

    async def scenario() -> None:
        http = IntegrationHttpClient(settings)
        try:
            github = GitHubClient(
                http=http,
                base_url=settings.github_base_url,
                credential=EnvironmentOrFileCredential(
                    value=settings.github_token,
                    path=settings.github_token_file,
                ),
                settings=settings,
            )
            jira = JiraClient(
                http=http,
                base_url=settings.jira_base_url,
                credential=EnvironmentOrFileCredential(
                    value=settings.jira_api_token,
                    path=settings.jira_api_token_file,
                ),
                settings=settings,
            )
            slack = SlackClient(
                http=http,
                base_url=settings.slack_base_url,
                credential=EnvironmentOrFileCredential(
                    value=settings.slack_bot_token,
                    path=settings.slack_bot_token_file,
                ),
                settings=settings,
            )
            assert await github.verify_identity(correlation_id="m4b-sandbox-read")
            assert await slack.verify_identity(correlation_id="m4b-sandbox-read")
            issue = await jira.fetch_issue(
                jira_issue_key,
                correlation_id="m4b-sandbox-read",
            )
            assert issue.key.casefold() == jira_issue_key.casefold()
        finally:
            await http.aclose()

    asyncio.run(scenario())


def test_real_provider_write_contract_requires_explicit_approval() -> None:
    settings = _require_sandbox(writes=True)
    repository = _required_environment("NINJATECH_SANDBOX_GITHUB_REPOSITORY")
    if not repository.casefold().startswith("amirss/"):
        pytest.skip("write repository is not under the confirmed amirss owner")
    issue_number = int(_required_environment("NINJATECH_SANDBOX_GITHUB_ISSUE_NUMBER"))
    channel = _required_environment("NINJATECH_SANDBOX_SLACK_CHANNEL_ID")
    if channel not in settings.deployment_allowed_slack_channels:
        pytest.skip("write channel is not allowlisted")
    marker = f"ninjatech-m4b-sandbox-{uuid4().hex[:12]}"

    async def scenario() -> None:
        http = IntegrationHttpClient(settings)
        try:
            github = GitHubClient(
                http=http,
                base_url=settings.github_base_url,
                credential=EnvironmentOrFileCredential(
                    value=settings.github_token,
                    path=settings.github_token_file,
                ),
                settings=settings,
            )
            slack = SlackClient(
                http=http,
                base_url=settings.slack_base_url,
                credential=EnvironmentOrFileCredential(
                    value=settings.slack_bot_token,
                    path=settings.slack_bot_token_file,
                ),
                settings=settings,
            )
            assert await github.verify_identity(correlation_id=marker)
            assert await slack.verify_identity(correlation_id=marker)
            comment = await github.create_comment(
                repository,
                issue_number,
                f"Checkpoint 4B sandbox verification. Marker: `{marker}`",
                correlation_id=marker,
            )
            receipt = await slack.post_notification(
                SlackMessageRequest(
                    channel=channel,
                    text=(
                        "Checkpoint 4B sandbox verification\n"
                        f"Authoritative GitHub: <{comment.url}|comment>\n"
                        f"Marker: {marker}"
                    ),
                ),
                correlation_id=marker,
            )
            assert comment.identifier
            assert receipt.channel == channel
        finally:
            await http.aclose()

    asyncio.run(scenario())
