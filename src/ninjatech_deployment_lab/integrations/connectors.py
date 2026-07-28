from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast
from urllib.parse import quote

from pydantic import ValidationError

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.credentials import CredentialProvider
from ninjatech_deployment_lab.integrations.domain import (
    GitHubRepositoryContext,
    JiraWorkItem,
    ServiceCatalogRecord,
    canonical_source_url,
)
from ninjatech_deployment_lab.integrations.http import (
    AmbiguousWriteError,
    IntegrationHttpClient,
    JsonHttpResponse,
    ProviderContractError,
    RetryableHttpError,
)
from ninjatech_deployment_lab.tasks.schemas import JsonValue


class ProviderError(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__("Provider operation failed")


class RetryableProviderError(ProviderError):
    def __init__(self, error_code: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(error_code)
        self.retry_after_seconds = retry_after_seconds


class PermanentProviderError(ProviderError):
    pass


class ProviderNotFoundError(PermanentProviderError):
    pass


class ProviderWriteOutcomeUnknown(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubComment:
    identifier: str
    body: str
    url: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CommentSearchResult:
    comments: tuple[GitHubComment, ...]
    complete: bool


class ServiceCatalogReader(Protocol):
    async def fetch_records(
        self,
        service_id: str,
        *,
        correlation_id: str,
    ) -> tuple[ServiceCatalogRecord, ...]: ...


class JiraReader(Protocol):
    async def fetch_issue(self, issue_key: str, *, correlation_id: str) -> JiraWorkItem: ...


class GitHubReaderWriter(Protocol):
    async def verify_identity(self, *, correlation_id: str) -> bool: ...

    async def fetch_context(
        self,
        repository: str,
        issue_number: int,
        *,
        correlation_id: str,
    ) -> GitHubRepositoryContext: ...

    async def get_comment(
        self,
        repository: str,
        comment_id: str,
        *,
        correlation_id: str,
    ) -> GitHubComment | None: ...

    async def find_comments_by_marker(
        self,
        repository: str,
        issue_number: int,
        marker: str,
        *,
        correlation_id: str,
    ) -> CommentSearchResult: ...

    async def create_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
        *,
        correlation_id: str,
    ) -> GitHubComment: ...

    async def update_comment(
        self,
        repository: str,
        comment_id: str,
        body: str,
        *,
        correlation_id: str,
    ) -> GitHubComment: ...


class _BaseConnector:
    def __init__(
        self,
        *,
        http: IntegrationHttpClient,
        base_url: str,
        credential: CredentialProvider,
        settings: Settings,
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._credential = credential
        self._max_retry_after = settings.integration_max_retry_after_seconds

    def _bearer_headers(self) -> dict[str, str]:
        secret = self._credential.get_secret()
        return {} if secret is None else {"Authorization": f"Bearer {secret}"}

    def _classify_read(self, response: JsonHttpResponse, *, not_found: str) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code == 404:
            raise ProviderNotFoundError(not_found)
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise RetryableProviderError(
                "provider_read_retryable",
                retry_after_seconds=_retry_after(
                    response.headers,
                    maximum=self._max_retry_after,
                ),
            )
        if response.status_code in {401, 403}:
            raise PermanentProviderError("provider_identity_rejected")
        raise PermanentProviderError("provider_read_rejected")


class ServiceCatalogClient(_BaseConnector):
    async def fetch_records(
        self,
        service_id: str,
        *,
        correlation_id: str,
    ) -> tuple[ServiceCatalogRecord, ...]:
        try:
            response = await self._http.request_json(
                method="GET",
                base_url=self._base_url,
                path=f"services?service_id={quote(service_id, safe='')}",
                headers=self._bearer_headers(),
                correlation_id=correlation_id,
            )
        except RetryableHttpError:
            raise RetryableProviderError("service_catalog_unavailable") from None
        except ProviderContractError:
            raise PermanentProviderError("service_catalog_contract_error") from None
        self._classify_read(response, not_found="service_not_found")
        payload = _object_payload(response.payload)
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise PermanentProviderError("service_catalog_contract_error")
        records: list[ServiceCatalogRecord] = []
        for raw in raw_records:
            records.append(_normalize_service_record(_object_payload(raw)))
        return tuple(records)


class JiraClient(_BaseConnector):
    def __init__(
        self,
        *,
        http: IntegrationHttpClient,
        base_url: str,
        credential: CredentialProvider,
        settings: Settings,
    ) -> None:
        super().__init__(
            http=http,
            base_url=base_url,
            credential=credential,
            settings=settings,
        )
        self._username = settings.jira_username

    def _jira_headers(self) -> dict[str, str]:
        if self._username is not None:
            return basic_auth_header(self._username, self._credential)
        return self._bearer_headers()

    async def fetch_issue(self, issue_key: str, *, correlation_id: str) -> JiraWorkItem:
        try:
            response = await self._http.request_json(
                method="GET",
                base_url=self._base_url,
                path=f"rest/api/3/issue/{quote(issue_key, safe='')}",
                headers=self._jira_headers(),
                correlation_id=correlation_id,
            )
        except RetryableHttpError:
            raise RetryableProviderError("jira_unavailable") from None
        except ProviderContractError:
            raise PermanentProviderError("jira_contract_error") from None
        self._classify_read(response, not_found="jira_issue_not_found")
        payload = _object_payload(response.payload)
        fields = _object_payload(payload.get("fields"))
        try:
            updated_at = datetime.fromisoformat(_required_string(fields, "updated"))
            source_url = canonical_source_url(_required_string(payload, "self"))
            description = jira_adf_to_text(fields.get("description"))
            normalized = {
                "key": _required_string(payload, "key"),
                "title": _required_string(fields, "summary"),
                "normalized_description_text": description,
                "status": _nested_name(fields.get("status")),
                "priority": _optional_nested_name(fields.get("priority")),
                "labels": tuple(_string_list(fields.get("labels"))),
                "assignee_identifier": _optional_assignee(fields.get("assignee")),
                "updated_at": updated_at,
                "source_url": source_url,
                "source_version": str(payload.get("version") or fields["updated"]),
            }
            return JiraWorkItem.model_validate(normalized)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise PermanentProviderError("jira_contract_error") from None


class GitHubClient(_BaseConnector):
    def __init__(
        self,
        *,
        http: IntegrationHttpClient,
        base_url: str,
        credential: CredentialProvider,
        settings: Settings,
    ) -> None:
        super().__init__(
            http=http,
            base_url=base_url,
            credential=credential,
            settings=settings,
        )
        self._expected_login = settings.github_expected_login
        self._max_pages = settings.integration_max_pages
        self._max_items = settings.integration_max_items
        self._write_timeout = settings.integration_provider_write_timeout_seconds

    def _github_headers(self) -> dict[str, str]:
        headers = self._bearer_headers()
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        return headers

    async def verify_identity(self, *, correlation_id: str) -> bool:
        """Verify the configured GitHub login using case-insensitive username semantics."""
        response = await self._read("user", correlation_id=correlation_id)
        try:
            payload = _object_payload(response.payload)
            login = _required_string(payload, "login")
        except (PermanentProviderError, ValueError):
            raise PermanentProviderError("github_contract_error") from None
        if self._expected_login is None:
            return False
        return login.casefold() == self._expected_login.casefold()

    async def fetch_context(
        self,
        repository: str,
        issue_number: int,
        *,
        correlation_id: str,
    ) -> GitHubRepositoryContext:
        repo_path = _repository_path(repository)
        repository_response = await self._read(
            f"repos/{repo_path}",
            correlation_id=correlation_id,
        )
        try:
            repository_payload = _object_payload(repository_response.payload)
            default_branch = _required_string(repository_payload, "default_branch")
        except (PermanentProviderError, ValueError):
            raise PermanentProviderError("github_contract_error") from None
        commit_response = await self._read(
            f"repos/{repo_path}/commits/{quote(default_branch, safe='')}",
            correlation_id=correlation_id,
        )
        issue_response = await self._read(
            f"repos/{repo_path}/issues/{issue_number}",
            correlation_id=correlation_id,
        )
        commit_payload = _object_payload(commit_response.payload)
        issue_payload = _object_payload(issue_response.payload)
        try:
            sha = _required_string(commit_payload, "sha")
            return GitHubRepositoryContext.model_validate(
                {
                    "full_name": _required_string(repository_payload, "full_name"),
                    "repository_id": _required_int(repository_payload, "id"),
                    "visibility": _required_string(repository_payload, "visibility"),
                    "archived": _required_bool(repository_payload, "archived"),
                    "default_branch": default_branch,
                    "default_branch_head_sha": sha,
                    "issue_number": _required_int(issue_payload, "number"),
                    "issue_state": _required_string(issue_payload, "state"),
                    "issue_title": _required_string(issue_payload, "title"),
                    "is_pull_request": "pull_request" in issue_payload,
                    "source_url": canonical_source_url(_required_string(issue_payload, "html_url")),
                    "source_version": (f"{sha}:{_required_string(issue_payload, 'updated_at')}"),
                }
            )
        except (TypeError, ValueError, ValidationError):
            raise PermanentProviderError("github_contract_error") from None

    async def get_comment(
        self,
        repository: str,
        comment_id: str,
        *,
        correlation_id: str,
    ) -> GitHubComment | None:
        try:
            response = await self._read(
                f"repos/{_repository_path(repository)}/issues/comments/"
                f"{quote(comment_id, safe='')}",
                correlation_id=correlation_id,
            )
        except ProviderNotFoundError:
            return None
        return _normalize_comment(response.payload, ambiguous_on_failure=False)

    async def find_comments_by_marker(
        self,
        repository: str,
        issue_number: int,
        marker: str,
        *,
        correlation_id: str,
    ) -> CommentSearchResult:
        matches: list[GitHubComment] = []
        seen = 0
        complete = True
        for page in range(1, self._max_pages + 1):
            response = await self._read(
                f"repos/{_repository_path(repository)}/issues/{issue_number}/comments"
                f"?per_page=100&page={page}",
                correlation_id=correlation_id,
            )
            if not isinstance(response.payload, list):
                raise PermanentProviderError("github_contract_error")
            comments = tuple(
                _normalize_comment(item, ambiguous_on_failure=False) for item in response.payload
            )
            seen += len(comments)
            matches.extend(comment for comment in comments if marker in comment.body)
            if len(comments) < 100:
                break
            if page == self._max_pages or seen >= self._max_items:
                complete = False
                break
        return CommentSearchResult(comments=tuple(matches), complete=complete)

    async def create_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
        *,
        correlation_id: str,
    ) -> GitHubComment:
        response = await self._write(
            "POST",
            f"repos/{_repository_path(repository)}/issues/{issue_number}/comments",
            {"body": body},
            correlation_id=correlation_id,
        )
        return _normalize_comment(response.payload, ambiguous_on_failure=True)

    async def update_comment(
        self,
        repository: str,
        comment_id: str,
        body: str,
        *,
        correlation_id: str,
    ) -> GitHubComment:
        response = await self._write(
            "PATCH",
            f"repos/{_repository_path(repository)}/issues/comments/{quote(comment_id, safe='')}",
            {"body": body},
            correlation_id=correlation_id,
        )
        return _normalize_comment(response.payload, ambiguous_on_failure=True)

    async def _read(self, path: str, *, correlation_id: str) -> JsonHttpResponse:
        try:
            response = await self._http.request_json(
                method="GET",
                base_url=self._base_url,
                path=path,
                headers=self._github_headers(),
                correlation_id=correlation_id,
            )
        except RetryableHttpError:
            raise RetryableProviderError("github_unavailable") from None
        except ProviderContractError:
            raise PermanentProviderError("github_contract_error") from None
        self._classify_read(response, not_found="github_resource_not_found")
        return response

    async def _write(
        self,
        method: str,
        path: str,
        payload: dict[str, JsonValue],
        *,
        correlation_id: str,
    ) -> JsonHttpResponse:
        try:
            response = await self._http.request_json(
                method=method,
                base_url=self._base_url,
                path=path,
                headers=self._github_headers(),
                json_body=payload,
                correlation_id=correlation_id,
                write=True,
                timeout_seconds=self._write_timeout,
            )
        except AmbiguousWriteError:
            raise ProviderWriteOutcomeUnknown("github_write_outcome_unknown") from None
        except RetryableHttpError:
            # Only connection establishment/pool failures are classified here,
            # so the request is known not to have been transmitted.
            raise RetryableProviderError("github_write_pretransmission_failure") from None
        except ProviderContractError:
            raise ProviderWriteOutcomeUnknown("github_write_outcome_unknown") from None

        if 200 <= response.status_code < 300:
            if response.payload is None:
                raise ProviderWriteOutcomeUnknown("github_write_outcome_unknown")
            return response
        if response.status_code == 429:
            raise RetryableProviderError(
                "github_rate_limited",
                retry_after_seconds=_retry_after(
                    response.headers,
                    maximum=self._max_retry_after,
                ),
            )
        if response.status_code == 408:
            raise ProviderWriteOutcomeUnknown("github_write_outcome_unknown")
        if response.status_code in {500, 502, 503, 504}:
            raise ProviderWriteOutcomeUnknown("github_write_outcome_unknown")
        if response.status_code in {401, 403}:
            raise PermanentProviderError("github_write_forbidden")
        if response.status_code == 404:
            raise PermanentProviderError("github_write_target_not_found")
        raise PermanentProviderError("github_write_rejected")


def jira_adf_to_text(value: JsonValue | object) -> str:
    """Flatten bounded Atlassian Document Format into deterministic plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:20000]
    if not isinstance(value, dict):
        raise ValueError("Jira description is neither text nor ADF")
    output: list[str] = []

    def visit(node: object, depth: int = 0) -> None:
        if depth > 40 or len("".join(output)) > 20000:
            return
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                output.append(text)
            content = node.get("content")
            if isinstance(content, list):
                for child in content[:500]:
                    visit(child, depth + 1)
            if node.get("type") in {"paragraph", "heading", "listItem"}:
                output.append("\n")
        elif isinstance(node, list):
            for child in node[:500]:
                visit(child, depth + 1)

    visit(value)
    return " ".join("".join(output).split())[:20000]


def _normalize_service_record(payload: Mapping[str, JsonValue]) -> ServiceCatalogRecord:
    def first(*names: str) -> JsonValue:
        for name in names:
            if name in payload and payload[name] is not None:
                return payload[name]
        return None

    try:
        service_id = _string_value(first("service_id", "serviceId", "legacy_id"))
        canonical_id = _string_value(first("canonical_service_id", "canonicalId", "service_id"))
        repositories = tuple(
            sorted(
                {
                    item.casefold()
                    for item in _string_list(
                        first("approved_repositories", "repositories", "repo_names")
                    )
                }
            )
        )
        aliases = tuple(sorted(set(_string_list(first("aliases", "legacy_ids")))))
        return ServiceCatalogRecord.model_validate(
            {
                "service_id": service_id,
                "canonical_service_id": canonical_id,
                "aliases": aliases,
                "service_owner": _optional_string(first("service_owner", "owner", "owner_id")),
                "criticality": _string_value(first("criticality", "tier", "service_tier")),
                "approved_repositories": repositories,
                "data_classification": _string_value(
                    first("data_classification", "classification")
                ).casefold(),
                "deployment_policy_version": _int_value(
                    first("deployment_policy_version", "policy_version")
                ),
                "automatic_publication_allowed": _bool_value(
                    first("automatic_publication_allowed", "auto_publish")
                ),
                "required_reviewer": _optional_string(first("required_reviewer", "reviewer")),
                "allow_automatic_updates": bool(first("allow_automatic_updates") or False),
                "source_version": _string_value(first("source_version", "etag", "version")),
                "source_url": canonical_source_url(_string_value(first("source_url", "url"))),
            }
        )
    except (TypeError, ValueError, ValidationError):
        raise PermanentProviderError("service_catalog_contract_error") from None


def _normalize_comment(
    payload: JsonValue | None,
    *,
    ambiguous_on_failure: bool,
) -> GitHubComment:
    try:
        value = _object_payload(payload)
        return GitHubComment(
            identifier=str(_required_int(value, "id")),
            body=_required_string(value, "body"),
            url=canonical_source_url(_required_string(value, "html_url")),
            updated_at=datetime.fromisoformat(_required_string(value, "updated_at")),
        )
    except (TypeError, ValueError, PermanentProviderError):
        if ambiguous_on_failure:
            raise ProviderWriteOutcomeUnknown("github_write_outcome_unknown") from None
        raise PermanentProviderError("github_contract_error") from None


def _object_payload(value: JsonValue | None) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise PermanentProviderError("provider_contract_error")
    return value


def _required_string(value: Mapping[str, JsonValue], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ValueError(name)
    return item


def _required_int(value: Mapping[str, JsonValue], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(name)
    return item


def _required_bool(value: Mapping[str, JsonValue], name: str) -> bool:
    item = value.get(name)
    if not isinstance(item, bool):
        raise ValueError(name)
    return item


def _nested_name(value: JsonValue | None) -> str:
    return _required_string(_object_payload(value), "name")


def _optional_nested_name(value: JsonValue | None) -> str | None:
    return None if value is None else _nested_name(value)


def _optional_assignee(value: JsonValue | None) -> str | None:
    if value is None:
        return None
    payload = _object_payload(value)
    for name in ("accountId", "key", "name"):
        item = payload.get(name)
        if isinstance(item, str) and item:
            return item
    return None


def _string_list(value: JsonValue | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("expected a string list")
    return cast(list[str], value)


def _string_value(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected non-empty string")
    return value


def _optional_string(value: JsonValue) -> str | None:
    return None if value is None else _string_value(value)


def _int_value(value: JsonValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("expected integer")
    return value


def _bool_value(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected boolean")
    return value


def _repository_path(repository: str) -> str:
    owner, name = repository.split("/", maxsplit=1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _retry_after(headers: Mapping[str, str], *, maximum: float) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return min(maximum, max(0.0, float(raw)))
    except ValueError:
        return None


def basic_auth_header(username: str, credential: CredentialProvider) -> dict[str, str]:
    secret = credential.get_secret()
    if secret is None:
        return {}
    encoded = base64.b64encode(f"{username}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def safe_marker(action_scope_key: str) -> str:
    digest = hashlib.sha256(action_scope_key.encode()).hexdigest()
    return f"<!-- ninjatech-deployment-context:{digest} -->"
