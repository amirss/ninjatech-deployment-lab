from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import pytest

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import (
    ClaimedTask,
    FenceResult,
    HeartbeatResult,
    RecoveryResult,
    RetryPolicy,
)
from ninjatech_deployment_lab.worker.handlers import (
    HandlerContext,
    HandlerRegistry,
    TaskExecution,
)
from ninjatech_deployment_lab.worker.repository import WorkerRepository
from ninjatech_deployment_lab.worker.runner import WorkerRunner


class LateReturnHandler:
    """Suppress one cancellation and return to exercise cause fencing."""

    async def execute(
        self,
        task: TaskExecution,
        context: HandlerContext,
    ) -> dict[str, JsonValue]:
        del task, context
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"late": True}
        raise AssertionError("unreachable")


class SuccessHandler:
    async def execute(
        self,
        task: TaskExecution,
        context: HandlerContext,
    ) -> dict[str, JsonValue]:
        del task, context
        return {"ok": True}


@dataclass
class FakeRepository:
    heartbeat_behavior: str = "active"
    success_calls: int = 0
    retry_calls: int = 0
    failure_calls: int = 0
    cancellation_calls: int = 0

    async def recover_one_expired(
        self,
        *,
        backoff_seconds: object,
    ) -> RecoveryResult | None:
        del backoff_seconds
        return None

    async def claim_one(self, **_: object) -> ClaimedTask | None:
        return None

    async def heartbeat(
        self,
        claim: ClaimedTask,
        *,
        lease_duration_seconds: float,
    ) -> HeartbeatResult:
        del claim, lease_duration_seconds
        if self.heartbeat_behavior == "error":
            raise RuntimeError("database failed with secret-never-log")
        if self.heartbeat_behavior == "cancel":
            return HeartbeatResult.CANCELLATION_REQUESTED
        return HeartbeatResult.ACTIVE

    async def finalize_success(
        self,
        claim: ClaimedTask,
        result: dict[str, JsonValue],
    ) -> FenceResult:
        del claim, result
        self.success_calls += 1
        return FenceResult.APPLIED

    async def schedule_retry(self, claim: ClaimedTask, **_: object) -> FenceResult:
        del claim
        self.retry_calls += 1
        return FenceResult.APPLIED

    async def finalize_failure(self, claim: ClaimedTask, **_: object) -> FenceResult:
        del claim
        self.failure_calls += 1
        return FenceResult.APPLIED

    async def finalize_cancellation(self, claim: ClaimedTask) -> FenceResult:
        del claim
        self.cancellation_calls += 1
        return FenceResult.APPLIED


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://user:password@127.0.0.1/test",
        "environment": "test",
        "worker_lease_duration_seconds": 1.0,
        "worker_heartbeat_interval_seconds": 0.1,
        "worker_handler_timeout_seconds": 0.3,
        "worker_shutdown_grace_seconds": 0.1,
        "worker_poll_interval_seconds": 0.05,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _claim(*, attempt_number: int = 1, max_attempts: int = 3) -> ClaimedTask:
    return ClaimedTask(
        task_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        worker_id="worker-secret-id",
        lease_token_hash="abcdef0123456789" * 4,
        task_type="test",
        task_input={"secret": "task-input-never-log"},
    )


def _runner(
    repository: FakeRepository,
    handler: LateReturnHandler | SuccessHandler,
    *,
    settings: Settings | None = None,
) -> WorkerRunner:
    registry = HandlerRegistry()
    registry.register("test", handler)
    return WorkerRunner(
        settings=settings or _settings(),
        repository=cast(WorkerRepository, repository),
        registry=registry,
        worker_id="worker-secret-id",
        retry_policy=RetryPolicy(0.01, 0.02, lambda: 0),
    )


def test_heartbeat_database_failure_blocks_late_success_and_new_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = FakeRepository(heartbeat_behavior="error")
    runner = _runner(repository, LateReturnHandler())
    caplog.set_level("WARNING")

    asyncio.run(runner._execute_claim(_claim()))

    assert repository.success_calls == 0
    assert repository.retry_calls == 0
    assert repository.failure_calls == 0
    assert repository.cancellation_calls == 0
    assert runner._stop_claiming is True
    captured = json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert "worker_ownership_lost" in captured
    assert "raw-token-never-log" not in captured
    assert ("abcdef0123456789" * 4) not in captured
    assert "abcdef012345" not in captured
    assert "task-input-never-log" not in captured
    assert "secret-never-log" not in captured


def test_late_return_after_timeout_cannot_succeed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = FakeRepository()
    runner = _runner(
        repository,
        LateReturnHandler(),
        settings=_settings(worker_handler_timeout_seconds=0.12),
    )
    caplog.set_level("WARNING")

    asyncio.run(runner._execute_claim(_claim()))

    assert repository.success_calls == 0
    assert repository.retry_calls == 1
    assert repository.cancellation_calls == 0
    assert "handler_timeout" in caplog.text


def test_timeout_at_max_attempts_fails_without_retry() -> None:
    repository = FakeRepository()
    runner = _runner(
        repository,
        LateReturnHandler(),
        settings=_settings(worker_handler_timeout_seconds=0.12),
    )

    asyncio.run(runner._execute_claim(_claim(attempt_number=2, max_attempts=2)))

    assert repository.success_calls == 0
    assert repository.retry_calls == 0
    assert repository.failure_calls == 1


def test_late_return_after_shutdown_grace_cannot_succeed() -> None:
    repository = FakeRepository()
    runner = _runner(repository, LateReturnHandler())
    runner.request_stop()

    asyncio.run(runner._execute_claim(_claim()))

    assert repository.success_calls == 0
    assert repository.retry_calls == 0
    assert repository.failure_calls == 0
    assert repository.cancellation_calls == 0


def test_customer_cancellation_persists_but_shutdown_does_not() -> None:
    cancellation_repository = FakeRepository(heartbeat_behavior="cancel")
    cancellation_runner = _runner(cancellation_repository, LateReturnHandler())
    asyncio.run(cancellation_runner._execute_claim(_claim()))

    shutdown_repository = FakeRepository()
    shutdown_runner = _runner(shutdown_repository, LateReturnHandler())
    shutdown_runner.request_stop()
    asyncio.run(shutdown_runner._execute_claim(_claim()))

    assert cancellation_repository.cancellation_calls == 1
    assert shutdown_repository.cancellation_calls == 0


def test_normal_result_succeeds() -> None:
    repository = FakeRepository()
    runner = _runner(repository, SuccessHandler())

    asyncio.run(runner._execute_claim(_claim()))

    assert repository.success_calls == 1
