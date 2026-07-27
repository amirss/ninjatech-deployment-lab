from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ninjatech_deployment_lab.tasks.domain import (
    IdempotencyConflictError,
    InvalidTaskTransitionError,
    TaskNotFoundError,
)
from ninjatech_deployment_lab.tasks.schemas import (
    ErrorResponse,
    IdempotencyKey,
    TaskCreateRequest,
    TaskResponse,
)
from ninjatech_deployment_lab.tasks.service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


async def get_task_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped SQLAlchemy session."""
    session_factory = cast(
        async_sessionmaker[AsyncSession],
        request.app.state.session_factory,
    )
    async with session_factory() as session:
        yield session


TaskSession = Annotated[AsyncSession, Depends(get_task_session)]
IdempotencyHeader = Annotated[IdempotencyKey, Header(alias="Idempotency-Key")]


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error={"code": code, "message": message}).model_dump(),
    )


async def task_not_found_handler(_: Request, error: Exception) -> JSONResponse:
    """Map a missing task to a stable 404 response."""
    task_error = cast(TaskNotFoundError, error)
    return _error_response(
        status.HTTP_404_NOT_FOUND,
        "task_not_found",
        str(task_error),
    )


async def idempotency_conflict_handler(
    _: Request,
    error: Exception,
) -> JSONResponse:
    """Map idempotency-key request mismatch to a stable 409 response."""
    conflict_error = cast(IdempotencyConflictError, error)
    return _error_response(
        status.HTTP_409_CONFLICT,
        "idempotency_conflict",
        str(conflict_error),
    )


async def invalid_transition_handler(
    _: Request,
    error: Exception,
) -> JSONResponse:
    """Map prohibited lifecycle commands to a stable 409 response."""
    transition_error = cast(InvalidTaskTransitionError, error)
    return _error_response(
        status.HTTP_409_CONFLICT,
        "invalid_task_transition",
        str(transition_error),
    )


def register_task_exception_handlers(application: FastAPI) -> None:
    """Register domain-to-HTTP mappings once during app construction."""
    application.add_exception_handler(TaskNotFoundError, task_not_found_handler)
    application.add_exception_handler(
        IdempotencyConflictError,
        idempotency_conflict_handler,
    )
    application.add_exception_handler(
        InvalidTaskTransitionError,
        invalid_transition_handler,
    )


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {"model": TaskResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def create_task(
    payload: TaskCreateRequest,
    response: Response,
    request: Request,
    session: TaskSession,
    idempotency_key: IdempotencyHeader,
) -> TaskResponse:
    """Create one task or replay the row bound to an identical request."""
    result = await TaskService(
        session,
        default_max_attempts=request.app.state.settings.worker_default_max_attempts,
    ).create_task(
        idempotency_key=idempotency_key,
        task_type=payload.task_type,
        task_input=payload.input,
    )
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return TaskResponse.model_validate(result.task)


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_task(task_id: UUID, session: TaskSession) -> TaskResponse:
    """Retrieve the latest committed state of one task."""
    task = await TaskService(session).get_task(task_id)
    return TaskResponse.model_validate(task)


@router.post(
    "/{task_id}/approve",
    response_model=TaskResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def approve_task(task_id: UUID, session: TaskSession) -> TaskResponse:
    """Approve an eligible task with idempotent repeated approval."""
    task = await TaskService(session).approve_task(task_id)
    return TaskResponse.model_validate(task)


@router.post(
    "/{task_id}/cancel",
    response_model=TaskResponse,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": TaskResponse,
            "description": "Cancellation requested for a running task",
        },
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def cancel_task(
    task_id: UUID,
    response: Response,
    session: TaskSession,
) -> TaskResponse:
    """Cancel an eligible task with idempotent repeated cancellation."""
    task = await TaskService(session).cancel_task(task_id)
    if task.status.value == "running":
        response.status_code = status.HTTP_202_ACCEPTED
    return TaskResponse.model_validate(task)
