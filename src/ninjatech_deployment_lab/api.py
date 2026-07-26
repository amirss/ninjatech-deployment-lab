from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import is_database_ready

router = APIRouter()


class StatusResponse(BaseModel):
    """Public status response shared by the foundation endpoints."""

    status: Literal["ok", "ready", "not_ready"]


@router.get("/health", response_model=StatusResponse)
async def health() -> StatusResponse:
    """Report process liveness without contacting external dependencies."""
    return StatusResponse(status="ok")


@router.get(
    "/ready",
    response_model=StatusResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": StatusResponse}},
)
async def ready(request: Request) -> StatusResponse | JSONResponse:
    """Report readiness only when the database responds within the configured timeout."""
    engine = cast(AsyncEngine, request.app.state.database_engine)
    settings = cast(Settings, request.app.state.settings)

    if await is_database_ready(engine, settings.db_ready_timeout_seconds):
        return StatusResponse(status="ready")

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=StatusResponse(status="not_ready").model_dump(),
    )
