from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.integrations.http import (
    AmbiguousWriteError,
    IntegrationHttpClient,
    ProviderContractError,
)
from ninjatech_deployment_lab.integrations.metrics import (
    MetricOperation,
    MetricProvider,
)


class _Response:
    def __init__(
        self,
        *,
        status_code: int,
        content_type: str,
        chunks: tuple[bytes, ...],
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _Stream:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        return None


class _Client:
    def __init__(self, response: _Response) -> None:
        self._response = response

    def stream(self, *args: object, **kwargs: object) -> _Stream:
        return _Stream(self._response)


def _settings(*, maximum_bytes: int = 1024) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        integration_max_response_bytes=maximum_bytes,
    )


def _with_response(response: _Response, *, maximum_bytes: int = 1024) -> IntegrationHttpClient:
    client = IntegrationHttpClient(_settings(maximum_bytes=maximum_bytes))
    asyncio.run(cast(Any, client)._client.aclose())
    cast(Any, client)._client = _Client(response)
    return client


def test_untrusted_redirect_style_path_is_rejected() -> None:
    with pytest.raises(ValueError):
        IntegrationHttpClient._trusted_url("https://trusted.example/api", "//evil.example")


def test_malformed_successful_write_is_outcome_unknown() -> None:
    client = _with_response(
        _Response(status_code=201, content_type="text/plain", chunks=(b"accepted",))
    )
    with pytest.raises(AmbiguousWriteError):
        asyncio.run(
            client.request_json(
                method="POST",
                base_url="https://trusted.example",
                path="comments",
                json_body={"body": "bounded"},
                write=True,
                provider=MetricProvider.GITHUB,
                operation=MetricOperation.WRITE,
            )
        )


def test_oversized_successful_write_is_outcome_unknown() -> None:
    client = _with_response(
        _Response(
            status_code=201,
            content_type="application/json",
            chunks=(b'{"body":"', b"x" * 2000, b'"}'),
        ),
        maximum_bytes=1024,
    )
    with pytest.raises(AmbiguousWriteError):
        asyncio.run(
            client.request_json(
                method="POST",
                base_url="https://trusted.example",
                path="comments",
                write=True,
                provider=MetricProvider.GITHUB,
                operation=MetricOperation.WRITE,
            )
        )


def test_malformed_successful_read_is_contract_failure() -> None:
    client = _with_response(
        _Response(status_code=200, content_type="text/plain", chunks=(b"not-json",))
    )
    with pytest.raises(ProviderContractError):
        asyncio.run(
            client.request_json(
                method="GET",
                base_url="https://trusted.example",
                path="resource",
                provider=MetricProvider.GITHUB,
                operation=MetricOperation.READ,
            )
        )


def test_error_response_does_not_require_or_expose_body() -> None:
    client = _with_response(
        _Response(status_code=429, content_type="text/plain", chunks=(b"secret detail",))
    )
    response = asyncio.run(
        client.request_json(
            method="GET",
            base_url="https://trusted.example",
            path="resource",
            provider=MetricProvider.GITHUB,
            operation=MetricOperation.READ,
        )
    )
    assert response.status_code == 429
    assert response.payload is None


def test_vendor_json_content_type_is_accepted() -> None:
    client = _with_response(
        _Response(
            status_code=200,
            content_type="application/vnd.github+json; charset=utf-8",
            chunks=(b'{"id":1}',),
        )
    )
    response = asyncio.run(
        client.request_json(
            method="GET",
            base_url="https://trusted.example",
            path="resource",
            provider=MetricProvider.GITHUB,
            operation=MetricOperation.READ,
        )
    )
    assert response.payload == {"id": 1}
