from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="NinjaTech Milestone 4 Provider Simulator")


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=32768)


class SlackMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(pattern=r"^[CG][A-Z0-9]{8,30}$")
    text: str = Field(min_length=1, max_length=4000)
    mrkdwn: bool
    unfurl_links: bool
    unfurl_media: bool


class SimulatorState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.comments: dict[int, dict[str, Any]] = {}
        self.next_comment_id = 1000
        self.create_calls = 0
        self.update_calls = 0
        self.catalog_calls = 0
        self.github_read_calls = 0
        self.jira_calls = 0
        self.delayed_acceptance_started = False
        self.slack_identity_calls = 0
        self.slack_post_calls = 0
        self.slack_messages: list[dict[str, str]] = []
        self.next_slack_timestamp = 1_722_000_000

    async def add_comment(
        self,
        *,
        repository: str,
        issue_number: int,
        body: str,
    ) -> dict[str, Any]:
        async with self.lock:
            identifier = self.next_comment_id
            self.next_comment_id += 1
            timestamp = datetime.now(UTC).isoformat()
            comment = {
                "id": identifier,
                "repository": repository.casefold(),
                "issue_number": issue_number,
                "body": body,
                "html_url": (
                    f"http://simulator:8090/github/{repository}/issues/"
                    f"{issue_number}#issuecomment-{identifier}"
                ),
                "updated_at": timestamp,
            }
            self.comments[identifier] = comment
            return comment

    async def add_slack_message(self, payload: SlackMessageBody) -> dict[str, str]:
        async with self.lock:
            timestamp = f"{self.next_slack_timestamp}.000001"
            self.next_slack_timestamp += 1
            message = {
                "channel": payload.channel,
                "timestamp": timestamp,
                "text": payload.text,
            }
            self.slack_messages.append(message)
            return message


state = SimulatorState()


@app.on_event("startup")
async def prevent_production_start() -> None:
    environment = os.getenv("NINJATECH_ENVIRONMENT", "development").casefold()
    if environment in {"staging", "production"}:
        raise RuntimeError("provider simulator is forbidden in staging and production")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/catalog/services")
async def catalog_services(service_id: str = Query(min_length=1)) -> dict[str, object]:
    state.catalog_calls += 1
    records = {
        "payments-api": [_service_record("payments-api")],
        "payments-legacy": [_service_record("payments-api")],
        "blocked-service": [
            _service_record(
                "blocked-service",
                classification="restricted",
                repositories=["customer/blocked-service"],
            )
        ],
        "missing-owner": [_service_record("missing-owner", owner=None)],
        "conflicting-service": [
            _service_record("conflicting-service", policy_version=7),
            _service_record("conflicting-service", policy_version=8),
        ],
    }
    return {"records": records.get(service_id.casefold(), [])}


@app.get("/jira/rest/api/3/issue/{issue_key}")
async def jira_issue(issue_key: str) -> dict[str, object]:
    state.jira_calls += 1
    if issue_key not in {"ENG-123", "ENG-124", "ENG-198", "ENG-199"}:
        raise HTTPException(status_code=404)
    updated = "2026-07-27T12:00:00+00:00"
    return {
        "key": issue_key,
        "self": f"http://simulator:8090/jira/rest/api/3/issue/{issue_key}",
        "version": "jira-v1",
        "fields": {
            "summary": "Prepare the bounded deployment context",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Deployment evidence."}],
                    }
                ],
            },
            "status": {"name": "In Progress"},
            "priority": {"name": "High"},
            "labels": ["deployment"],
            "assignee": {"accountId": "user-123"},
            "updated": updated,
        },
    }


@app.get("/github/user")
async def github_user() -> dict[str, str]:
    state.github_read_calls += 1
    return {"login": "simulator-bot"}


@app.get("/github/repos/{owner}/{repository}")
async def github_repository(owner: str, repository: str) -> dict[str, object]:
    state.github_read_calls += 1
    return {
        "id": 424242,
        "full_name": f"{owner}/{repository}",
        "visibility": "private",
        "archived": repository.casefold() == "archived-service",
        "default_branch": "main",
    }


@app.get("/github/repos/{owner}/{repository}/commits/{reference}")
async def github_commit(owner: str, repository: str, reference: str) -> dict[str, str]:
    state.github_read_calls += 1
    return {"sha": "a" * 40}


@app.get("/github/repos/{owner}/{repository}/issues/{issue_number}")
async def github_issue(owner: str, repository: str, issue_number: int) -> dict[str, object]:
    state.github_read_calls += 1
    return {
        "number": issue_number,
        "state": "closed" if issue_number == 97 else "open",
        "title": "Deployment context request",
        "html_url": (f"http://simulator:8090/github/{owner}/{repository}/issues/{issue_number}"),
        "updated_at": "2026-07-27T12:00:00+00:00",
    }


@app.get("/github/repos/{owner}/{repository}/issues/{issue_number}/comments")
async def list_comments(
    owner: str,
    repository: str,
    issue_number: int,
    per_page: int = Query(default=100, ge=1, le=100),
    page: int = Query(default=1, ge=1),
) -> list[dict[str, Any]]:
    state.github_read_calls += 1
    repo = f"{owner}/{repository}".casefold()
    matching = [
        _public_comment(comment)
        for comment in state.comments.values()
        if comment["repository"] == repo and comment["issue_number"] == issue_number
    ]
    start = (page - 1) * per_page
    return matching[start : start + per_page]


@app.post(
    "/github/repos/{owner}/{repository}/issues/{issue_number}/comments",
    status_code=201,
    response_model=None,
)
async def create_comment(
    owner: str,
    repository: str,
    issue_number: int,
    payload: CommentBody,
) -> dict[str, Any] | Response:
    state.create_calls += 1
    repository_name = f"{owner}/{repository}"
    if issue_number == 198:
        comment = await state.add_comment(
            repository=repository_name,
            issue_number=issue_number,
            body=payload.body,
        )
        return Response(
            content="accepted-but-malformed",
            status_code=201,
            media_type="text/plain",
            headers={"X-Simulator-Comment-ID": str(comment["id"])},
        )
    if issue_number == 199 and not state.delayed_acceptance_started:
        state.delayed_acceptance_started = True
        accepted = asyncio.create_task(
            _delayed_comment(
                repository=repository_name,
                issue_number=issue_number,
                body=payload.body,
            )
        )
        await asyncio.sleep(float(os.getenv("SIMULATOR_DELAYED_RESPONSE_SECONDS", "10")))
        return _public_comment(await accepted)
    comment = await state.add_comment(
        repository=repository_name,
        issue_number=issue_number,
        body=payload.body,
    )
    return _public_comment(comment)


@app.get("/github/repos/{owner}/{repository}/issues/comments/{comment_id}")
async def get_comment(owner: str, repository: str, comment_id: int) -> dict[str, Any]:
    state.github_read_calls += 1
    comment = state.comments.get(comment_id)
    if comment is None or comment["repository"] != f"{owner}/{repository}".casefold():
        raise HTTPException(status_code=404)
    return _public_comment(comment)


@app.patch("/github/repos/{owner}/{repository}/issues/comments/{comment_id}")
async def update_comment(
    owner: str,
    repository: str,
    comment_id: int,
    payload: CommentBody,
) -> dict[str, Any]:
    state.update_calls += 1
    async with state.lock:
        comment = state.comments.get(comment_id)
        if comment is None or comment["repository"] != f"{owner}/{repository}".casefold():
            raise HTTPException(status_code=404)
        comment["body"] = payload.body
        comment["updated_at"] = datetime.now(UTC).isoformat()
        return _public_comment(comment)


@app.post("/slack/auth.test")
async def slack_auth_test(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    state.slack_identity_calls += 1
    if authorization is None or authorization.endswith("simulator-revoked-token"):
        return {"ok": False, "error": "invalid_auth"}
    team_id = (
        "T0000000000" if authorization.endswith("simulator-wrong-team-token") else "T1234567890"
    )
    user_id = (
        "U0000000000" if authorization.endswith("simulator-wrong-user-token") else "U1234567890"
    )
    bot_id = "B0000000000" if authorization.endswith("simulator-wrong-bot-token") else "B1234567890"
    return {
        "ok": True,
        "team_id": team_id,
        "user_id": user_id,
        "bot_id": bot_id,
        "team": "Fictional Simulator",
    }


@app.post("/slack/chat.postMessage", response_model=None)
async def slack_post_message(payload: SlackMessageBody) -> dict[str, object] | Response:
    state.slack_post_calls += 1
    if payload.mrkdwn is not True or payload.unfurl_links or payload.unfurl_media:
        return {"ok": False, "error": "invalid_arguments"}
    if payload.channel == "CRATELIMIT1":
        return Response(
            content='{"ok":false,"error":"ratelimited"}',
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": "1"},
        )
    if payload.channel == "CPERMFAIL01":
        return {"ok": False, "error": "channel_not_found"}

    message = await state.add_slack_message(payload)
    if payload.channel == "CUNKNOWN001":
        return Response(
            content="accepted-but-malformed",
            status_code=200,
            media_type="text/plain",
        )
    if payload.channel == "CDELAYED001":
        await asyncio.sleep(float(os.getenv("SIMULATOR_SLACK_RESPONSE_DELAY_SECONDS", "10")))
    return {
        "ok": True,
        "channel": message["channel"],
        "ts": message["timestamp"],
    }


@app.get("/__simulator/evidence")
async def evidence() -> dict[str, object]:
    return {
        "catalog_calls": state.catalog_calls,
        "github_read_calls": state.github_read_calls,
        "jira_calls": state.jira_calls,
        "create_calls": state.create_calls,
        "update_calls": state.update_calls,
        "comment_count": len(state.comments),
        "slack_identity_calls": state.slack_identity_calls,
        "slack_post_calls": state.slack_post_calls,
        "slack_message_count": len(state.slack_messages),
    }


async def _delayed_comment(
    *,
    repository: str,
    issue_number: int,
    body: str,
) -> dict[str, Any]:
    await asyncio.sleep(float(os.getenv("SIMULATOR_ACCEPT_DELAY_SECONDS", "4")))
    return await state.add_comment(
        repository=repository,
        issue_number=issue_number,
        body=body,
    )


def _service_record(
    service_id: str,
    *,
    owner: str | None = "team-payments",
    classification: str = "internal",
    repositories: list[str] | None = None,
    policy_version: int = 7,
) -> dict[str, object]:
    return {
        "serviceId": service_id,
        "canonicalId": service_id,
        "legacy_ids": ["payments-legacy"] if service_id == "payments-api" else [],
        "owner": owner,
        "tier": "tier-1",
        "repo_names": repositories or ["Customer/Example-Service"],
        "classification": classification,
        "policy_version": policy_version,
        "auto_publish": True,
        "reviewer": None,
        "allow_automatic_updates": True,
        "etag": f"{service_id}-policy-{policy_version}",
        "url": f"http://simulator:8090/catalog/services/{service_id}?internal=true",
    }


def _public_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": comment["id"],
        "body": comment["body"],
        "html_url": comment["html_url"],
        "updated_at": comment["updated_at"],
    }
