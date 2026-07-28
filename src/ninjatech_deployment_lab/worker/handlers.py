from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import (
    ExecutionFence,
    OwnershipLostError,
    TaskCancelled,
)


@dataclass(frozen=True, slots=True)
class TaskExecution:
    """Handler-facing task data without database mutation capabilities."""

    task_id: UUID
    task_type: str
    task_input: dict[str, JsonValue]
    attempt_id: UUID
    attempt_number: int
    max_attempts: int
    execution_fence: ExecutionFence


class HandlerContext:
    """Cancellation-aware execution context with no direct persistence access."""

    def __init__(
        self,
        *,
        task_id: UUID,
        attempt_id: UUID,
        attempt_number: int,
        worker_id: str,
        customer_cancellation: asyncio.Event,
        ownership_lost: asyncio.Event,
    ) -> None:
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.attempt_number = attempt_number
        self.worker_id = worker_id
        self._customer_cancellation = customer_cancellation
        self._ownership_lost = ownership_lost

    def raise_if_cancelled(self) -> None:
        """Raise the correct safe signal at a cooperative checkpoint."""
        self.raise_if_ownership_lost()
        if self._customer_cancellation.is_set():
            raise TaskCancelled

    def raise_if_ownership_lost(self) -> None:
        """Block persistence after certain or uncertain execution ownership loss."""
        if self._ownership_lost.is_set():
            raise OwnershipLostError

    async def sleep(self, seconds: float) -> None:
        """Sleep without delaying cooperative cancellation observation."""
        self.raise_if_cancelled()
        customer_wait = asyncio.create_task(self._customer_cancellation.wait())
        ownership_wait = asyncio.create_task(self._ownership_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {customer_wait, ownership_wait},
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done:
                self.raise_if_cancelled()
        finally:
            customer_wait.cancel()
            ownership_wait.cancel()


class TaskHandler(Protocol):
    """Small typed interface implemented by explicitly registered handlers."""

    async def execute(
        self,
        task: TaskExecution,
        context: HandlerContext,
    ) -> dict[str, JsonValue]: ...


class HandlerRegistry:
    """Explicit task-type-to-handler mapping without plugin discovery."""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_type: str, handler: TaskHandler) -> None:
        if task_type in self._handlers:
            raise ValueError(f"handler already registered for {task_type}")
        self._handlers[task_type] = handler

    def get(self, task_type: str) -> TaskHandler:
        try:
            return self._handlers[task_type]
        except KeyError:
            raise KeyError(f"no handler registered for {task_type}") from None

    @property
    def supported_task_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))
