from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ninjatech_deployment_lab.code_proposals.domain import (
    ModelAction,
    ModelProviderName,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from ninjatech_deployment_lab.code_proposals.prompting import (
    canonical_json_bytes,
    model_request_fingerprint,
    sha256_canonical,
)
from ninjatech_deployment_lab.code_proposals.providers import ModelProviderContractError


@dataclass(frozen=True, slots=True)
class RecordedExchange:
    expected_request_fingerprint: str
    response: object
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RecordedFixtureSet:
    name: str
    model_name: str
    exchanges: tuple[RecordedExchange, ...]


class RecordedFixtureCatalog:
    """Trusted fixture sets selected by bounded configuration, never task input."""

    def __init__(self, fixtures: tuple[RecordedFixtureSet, ...]) -> None:
        self._fixtures = {fixture.name: fixture for fixture in fixtures}
        if len(self._fixtures) != len(fixtures):
            raise ValueError("recorded fixture-set names must be unique")

    def get(self, name: str) -> RecordedFixtureSet:
        try:
            return self._fixtures[name]
        except KeyError:
            raise ValueError("unknown recorded fixture set") from None


class _RecordedExchangeFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: object
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class _RecordedFixtureFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    model_name: str = Field(min_length=1, max_length=255)
    exchanges: tuple[_RecordedExchangeFile, ...] = Field(min_length=1, max_length=50)


def load_recorded_fixture_set(
    name: str,
    *,
    trusted_fixture_directory: Path,
) -> RecordedFixtureSet:
    """Load one allowlisted repository fixture; only its bounded name is variable."""
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name) is None:
        raise ValueError("invalid recorded fixture-set name")
    path = trusted_fixture_directory / f"{name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        parsed = _RecordedFixtureFile.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError):
        raise ValueError("recorded fixture set is unavailable or malformed") from None
    if parsed.name != name:
        raise ValueError("recorded fixture-set identity mismatch")
    return RecordedFixtureSet(
        name=parsed.name,
        model_name=parsed.model_name,
        exchanges=tuple(
            RecordedExchange(
                expected_request_fingerprint=item.expected_request_fingerprint,
                response=item.response,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
            )
            for item in parsed.exchanges
        ),
    )


class RecordedModelProvider:
    """Deterministic provider indexed by persisted completed-response count."""

    def __init__(self, *, fixture_set: RecordedFixtureSet) -> None:
        self._fixture_set = fixture_set

    @property
    def provider(self) -> ModelProviderName:
        return ModelProviderName.RECORDED

    @property
    def model_name(self) -> str:
        return self._fixture_set.model_name

    async def complete(self, request: ModelRequest) -> ModelResponse:
        index = request.completed_response_count
        if index >= len(self._fixture_set.exchanges):
            raise ModelProviderContractError("recorded fixture sequence exhausted")
        exchange = self._fixture_set.exchanges[index]
        actual = model_request_fingerprint(request)
        if actual != exchange.expected_request_fingerprint:
            raise ModelProviderContractError("recorded request fingerprint mismatch")
        try:
            action: ModelAction = TypeAdapter(ModelAction).validate_python(exchange.response)
        except ValidationError:
            raise ModelProviderContractError("recorded response contract failure") from None
        response_payload = action.model_dump(mode="json")
        response_bytes = canonical_json_bytes(response_payload)
        return ModelResponse(
            action=action,
            usage=ModelUsage(
                input_tokens=exchange.input_tokens,
                output_tokens=exchange.output_tokens,
            ),
            response_fingerprint=sha256_canonical(response_payload),
            response_size_bytes=len(response_bytes),
        )

    async def aclose(self) -> None:
        return None
