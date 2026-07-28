from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urljoin, urlsplit

import httpx2

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.tasks.schemas import JsonValue


class IntegrationHttpError(Exception):
    """Sanitized HTTP boundary error."""

    error_code = "integration_http_error"


class RetryableHttpError(IntegrationHttpError):
    error_code = "provider_retryable_error"


class ProviderContractError(IntegrationHttpError):
    error_code = "provider_contract_error"


class AmbiguousWriteError(IntegrationHttpError):
    """A write may have reached the provider and must be reconciled."""

    error_code = "provider_write_outcome_unknown"


class ResponseTooLargeError(ProviderContractError):
    error_code = "provider_response_too_large"


@dataclass(frozen=True, slots=True)
class JsonHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    payload: JsonValue | None


class IntegrationHttpClient:
    """Only module allowed to expose httpx2 internally."""

    def __init__(self, settings: Settings) -> None:
        self._maximum_bytes = settings.integration_max_response_bytes
        self._client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(
                connect=settings.integration_http_connect_timeout_seconds,
                read=settings.integration_http_read_timeout_seconds,
                write=settings.integration_http_write_timeout_seconds,
                pool=settings.integration_http_pool_timeout_seconds,
            ),
            headers={"User-Agent": "ninjatech-deployment-lab/0.1"},
            follow_redirects=False,
            verify=True,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request_json(
        self,
        *,
        method: str,
        base_url: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        json_body: dict[str, JsonValue] | None = None,
        correlation_id: str | None = None,
        write: bool = False,
        timeout_seconds: float | None = None,
    ) -> JsonHttpResponse:
        url = self._trusted_url(base_url, path)
        request_headers = dict(headers or {})
        if correlation_id is not None:
            request_headers["X-Correlation-ID"] = correlation_id
        try:
            async with self._client.stream(
                method,
                url,
                headers=request_headers,
                json=json_body,
                timeout=timeout_seconds,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._maximum_bytes:
                        if write:
                            raise AmbiguousWriteError
                        raise ResponseTooLargeError

                response_headers = MappingProxyType(
                    {key.lower(): value for key, value in response.headers.items()}
                )
                payload: JsonValue | None
                if not 200 <= response.status_code < 300:
                    payload = None
                elif not body:
                    payload = None
                elif not _is_json_content_type(response_headers.get("content-type", "")):
                    if write and 200 <= response.status_code < 300:
                        raise AmbiguousWriteError
                    raise ProviderContractError
                else:
                    try:
                        payload = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        if write and 200 <= response.status_code < 300:
                            raise AmbiguousWriteError from None
                        raise ProviderContractError from None
                return JsonHttpResponse(
                    status_code=response.status_code,
                    headers=response_headers,
                    payload=payload,
                )
        except (AmbiguousWriteError, ProviderContractError):
            raise
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            raise RetryableHttpError from None
        except (
            httpx2.ReadError,
            httpx2.ReadTimeout,
            httpx2.RemoteProtocolError,
            httpx2.WriteError,
            httpx2.WriteTimeout,
        ):
            if write:
                raise AmbiguousWriteError from None
            raise RetryableHttpError from None

    @staticmethod
    def _trusted_url(base_url: str, path: str) -> str:
        if path.startswith("//"):
            raise ValueError("provider path must not replace the configured host")
        normalized_base = f"{base_url.rstrip('/')}/"
        url = urljoin(normalized_base, path.lstrip("/"))
        base = urlsplit(normalized_base)
        parsed = urlsplit(url)
        if (
            parsed.scheme != base.scheme
            or parsed.hostname != base.hostname
            or parsed.port != base.port
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("provider URL escaped the configured host")
        return url


def _is_json_content_type(value: str) -> bool:
    media_type = value.partition(";")[0].strip().casefold()
    return media_type == "application/json" or media_type.endswith("+json")
