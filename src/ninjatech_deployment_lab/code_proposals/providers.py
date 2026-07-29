from __future__ import annotations

from typing import Protocol

from ninjatech_deployment_lab.code_proposals.domain import (
    ModelProviderName,
    ModelRequest,
    ModelResponse,
)


class ModelProvider(Protocol):
    @property
    def provider(self) -> ModelProviderName: ...

    @property
    def model_name(self) -> str: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    async def aclose(self) -> None: ...


class ModelProviderError(Exception):
    """Sanitized model-provider failure base."""


class RetryableModelProviderError(ModelProviderError):
    """Future network/cost request may be retried; customer systems were not mutated."""


class ModelProviderContractError(ModelProviderError):
    """Provider or recorded fixture violated the normalized response contract."""
