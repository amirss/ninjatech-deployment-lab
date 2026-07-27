from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ninjatech_deployment_lab.worker.diagnostic import (
    DiagnosticHandler,
    diagnostic_input_adapter,
)
from ninjatech_deployment_lab.worker.domain import PermanentTaskError, RetryableTaskError
from ninjatech_deployment_lab.worker.handlers import HandlerContext, TaskExecution


def _context() -> HandlerContext:
    return HandlerContext(
        task_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        worker_id="unit-worker",
        customer_cancellation=asyncio.Event(),
        ownership_lost=asyncio.Event(),
    )


def _task(task_input: dict[str, object], *, attempt_number: int = 1) -> TaskExecution:
    return TaskExecution(
        task_id=uuid4(),
        task_type="diagnostic",
        task_input=task_input,  # type: ignore[arg-type]
        attempt_id=uuid4(),
        attempt_number=attempt_number,
        max_attempts=3,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"mode": "unknown"},
        {"mode": "success", "extra": True},
        {"mode": "delay", "duration_seconds": 0},
        {"mode": "retry_then_success", "failures": 0},
    ],
)
def test_diagnostic_input_is_strict(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        diagnostic_input_adapter.validate_python(payload)


def test_diagnostic_success() -> None:
    result = asyncio.run(DiagnosticHandler().execute(_task({"mode": "success"}), _context()))

    assert result == {"diagnostic": "succeeded", "attempt": 1}


def test_diagnostic_retry_then_success() -> None:
    handler = DiagnosticHandler()
    with pytest.raises(RetryableTaskError):
        asyncio.run(
            handler.execute(
                _task({"mode": "retry_then_success", "failures": 1}),
                _context(),
            )
        )

    result = asyncio.run(
        handler.execute(
            _task(
                {"mode": "retry_then_success", "failures": 1},
                attempt_number=2,
            ),
            _context(),
        )
    )
    assert result["diagnostic"] == "retry_succeeded"


def test_diagnostic_permanent_failure() -> None:
    with pytest.raises(PermanentTaskError):
        asyncio.run(
            DiagnosticHandler().execute(
                _task({"mode": "permanent_failure"}),
                _context(),
            )
        )
