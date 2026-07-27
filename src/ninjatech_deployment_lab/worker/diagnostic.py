from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import PermanentTaskError, RetryableTaskError
from ninjatech_deployment_lab.worker.handlers import HandlerContext, TaskExecution


class _DiagnosticBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosticSuccess(_DiagnosticBase):
    mode: Literal["success"]


class DiagnosticDelay(_DiagnosticBase):
    mode: Literal["delay"]
    duration_seconds: float = Field(gt=0, le=30)


class DiagnosticRetry(_DiagnosticBase):
    mode: Literal["retry_then_success"]
    failures: int = Field(ge=1, le=20)


class DiagnosticPermanentFailure(_DiagnosticBase):
    mode: Literal["permanent_failure"]


class DiagnosticTimeout(_DiagnosticBase):
    mode: Literal["timeout"]
    duration_seconds: float = Field(gt=0, le=30)


class DiagnosticWaitForCancellation(_DiagnosticBase):
    mode: Literal["wait_for_cancellation"]
    checkpoint_seconds: float = Field(default=0.05, ge=0.01, le=1)


DiagnosticInput = Annotated[
    DiagnosticSuccess
    | DiagnosticDelay
    | DiagnosticRetry
    | DiagnosticPermanentFailure
    | DiagnosticTimeout
    | DiagnosticWaitForCancellation,
    Field(discriminator="mode"),
]

diagnostic_input_adapter: TypeAdapter[DiagnosticInput] = TypeAdapter(DiagnosticInput)


class DiagnosticHandler:
    """Deterministic non-production handler with no shell, file, or network access."""

    async def execute(
        self,
        task: TaskExecution,
        context: HandlerContext,
    ) -> dict[str, JsonValue]:
        payload = diagnostic_input_adapter.validate_python(task.task_input)
        if isinstance(payload, DiagnosticSuccess):
            return {"diagnostic": "succeeded", "attempt": task.attempt_number}
        if isinstance(payload, DiagnosticDelay):
            await context.sleep(payload.duration_seconds)
            return {"diagnostic": "delayed_success", "attempt": task.attempt_number}
        if isinstance(payload, DiagnosticRetry):
            if task.attempt_number <= payload.failures:
                raise RetryableTaskError("diagnostic_retry")
            return {"diagnostic": "retry_succeeded", "attempt": task.attempt_number}
        if isinstance(payload, DiagnosticPermanentFailure):
            raise PermanentTaskError("diagnostic_permanent_failure")
        if isinstance(payload, DiagnosticTimeout):
            await context.sleep(payload.duration_seconds)
            return {"diagnostic": "timeout_returned", "attempt": task.attempt_number}

        while True:
            await context.sleep(payload.checkpoint_seconds)
