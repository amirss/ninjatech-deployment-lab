from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import create_database_engine, create_session_factory
from ninjatech_deployment_lab.main import create_app
from ninjatech_deployment_lab.tasks.domain import AttemptStatus, TaskStatus
from ninjatech_deployment_lab.tasks.model import Task, TaskAttempt
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.diagnostic import DiagnosticHandler
from ninjatech_deployment_lab.worker.domain import (
    ExecutionInvariantError,
    FenceResult,
    HeartbeatResult,
    RetryPolicy,
)
from ninjatech_deployment_lab.worker.handlers import HandlerRegistry
from ninjatech_deployment_lab.worker.repository import WorkerRepository
from ninjatech_deployment_lab.worker.runner import WorkerRunner

pytestmark = pytest.mark.postgres


def _settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "environment": "test",
        "worker_poll_interval_seconds": 0.05,
        "worker_lease_duration_seconds": 1.0,
        "worker_heartbeat_interval_seconds": 0.1,
        "worker_handler_timeout_seconds": 0.2,
        "worker_shutdown_grace_seconds": 0.1,
        "worker_retry_base_seconds": 0.02,
        "worker_retry_cap_seconds": 0.05,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _create_and_approve(
    database_url: str,
    *,
    key: str,
    task_type: str = "diagnostic",
    task_input: dict[str, JsonValue] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    application_settings = settings or _settings(database_url)
    payload = task_input or {"mode": "success"}
    with TestClient(create_app(application_settings)) as client:
        created = client.post(
            "/tasks",
            headers={"Idempotency-Key": key},
            json={"task_type": task_type, "input": payload},
        )
        assert created.status_code == 201
        approved = client.post(f"/tasks/{created.json()['id']}/approve")
        assert approved.status_code == 200
        return cast(dict[str, Any], approved.json())


async def _load_task(database_url: str, task_id: str) -> Task:
    engine = create_database_engine(_settings(database_url))
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            task = await session.get(Task, UUID(task_id))
            assert task is not None
            return task
    finally:
        await engine.dispose()


async def _load_attempts(database_url: str, task_id: str) -> list[TaskAttempt]:
    engine = create_database_engine(_settings(database_url))
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            result = await session.execute(
                select(TaskAttempt)
                .where(TaskAttempt.task_id == UUID(task_id))
                .order_by(TaskAttempt.attempt_number)
            )
            return list(result.scalars())
    finally:
        await engine.dispose()


async def _expire_lease(database_url: str, task_id: UUID) -> None:
    engine = create_database_engine(_settings(database_url))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(lease_expires_at=text("clock_timestamp() - interval '1 second'"))
            )
    finally:
        await engine.dispose()


def test_initial_approval_sets_available_at_and_is_immediately_claimable(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(
        postgres_database_url,
        key="worker-initial-approval",
    )
    assert approved["available_at"] is not None

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            repository = WorkerRepository(create_session_factory(engine))
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="approval-worker",
                lease_duration_seconds=1,
            )
            assert claim is not None
            assert str(claim.task_id) == approved["id"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_repeated_approval_preserves_available_at_and_updated_at(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(
        postgres_database_url,
        key="worker-repeated-approval",
    )
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        replay = client.post(f"/tasks/{approved['id']}/approve")

    assert replay.status_code == 200
    assert replay.json()["available_at"] == approved["available_at"]
    assert replay.json()["updated_at"] == approved["updated_at"]


def test_two_workers_claim_different_tasks_concurrently(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    first = _create_and_approve(postgres_database_url, key="worker-two-first")
    second = _create_and_approve(postgres_database_url, key="worker-two-second")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            repository = WorkerRepository(create_session_factory(engine))
            claims = await asyncio.gather(
                repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="worker-a",
                    lease_duration_seconds=1,
                ),
                repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="worker-b",
                    lease_duration_seconds=1,
                ),
            )
            assert all(claim is not None for claim in claims)
            assert {str(claim.task_id) for claim in claims if claim is not None} == {
                first["id"],
                second["id"],
            }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_two_workers_cannot_claim_same_task_and_claim_is_committed(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(postgres_database_url, key="worker-one-claim")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            factory = create_session_factory(engine)
            repository = WorkerRepository(factory)
            claims = await asyncio.gather(
                repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="worker-a",
                    lease_duration_seconds=1,
                ),
                repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="worker-b",
                    lease_duration_seconds=1,
                ),
            )
            assert sum(claim is not None for claim in claims) == 1
            claim = next(claim for claim in claims if claim is not None)

            async with factory() as session:
                persisted_task = await session.get(Task, claim.task_id)
                persisted_attempt = await session.get(TaskAttempt, claim.attempt_id)
                assert persisted_task is not None
                assert persisted_task.status == TaskStatus.RUNNING
                assert persisted_attempt is not None
                assert persisted_attempt.status == AttemptStatus.RUNNING
                assert str(persisted_task.id) == approved["id"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_heartbeat_extends_active_lease_and_wrong_token_cannot_update(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    _create_and_approve(postgres_database_url, key="worker-heartbeat")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            factory = create_session_factory(engine)
            repository = WorkerRepository(factory)
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="heartbeat-worker",
                lease_duration_seconds=1,
            )
            assert claim is not None
            async with factory() as session:
                before = await session.get(Task, claim.task_id)
                assert before is not None and before.lease_expires_at is not None
                initial_expiration = before.lease_expires_at

            await asyncio.sleep(0.02)
            assert (
                await repository.heartbeat(claim, lease_duration_seconds=2)
                == HeartbeatResult.ACTIVE
            )
            async with factory() as session:
                after = await session.get(Task, claim.task_id)
                attempt = await session.get(TaskAttempt, claim.attempt_id)
                assert after is not None and after.lease_expires_at is not None
                assert after.lease_expires_at > initial_expiration
                assert attempt is not None
                heartbeat_at = attempt.last_heartbeat_at

            stale = replace(claim, lease_token_hash="0" * 64)
            with pytest.raises(ExecutionInvariantError):
                await repository.heartbeat(stale, lease_duration_seconds=2)
            async with factory() as session:
                attempt = await session.get(TaskAttempt, claim.attempt_id)
                assert attempt is not None
                assert attempt.last_heartbeat_at == heartbeat_at
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_finalization_is_atomic_with_exact_attempt(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    _create_and_approve(postgres_database_url, key="worker-finalize-atomic")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            factory = create_session_factory(engine)
            repository = WorkerRepository(factory)
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="atomic-worker",
                lease_duration_seconds=2,
            )
            assert claim is not None
            async with factory() as session, session.begin():
                attempt = await session.get(TaskAttempt, claim.attempt_id)
                assert attempt is not None
                await session.delete(attempt)

            with pytest.raises(ExecutionInvariantError):
                await repository.finalize_success(claim, {"ok": True})
            async with factory() as session:
                task = await session.get(Task, claim.task_id)
                assert task is not None
                assert task.status == TaskStatus.RUNNING
                assert task.result is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_recovery_is_atomic_with_attempt_history(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    _create_and_approve(postgres_database_url, key="worker-recovery-atomic")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            factory = create_session_factory(engine)
            repository = WorkerRepository(factory)
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="recovery-worker",
                lease_duration_seconds=1,
            )
            assert claim is not None
            await _expire_lease(postgres_database_url, claim.task_id)
            async with factory() as session, session.begin():
                attempt = await session.get(TaskAttempt, claim.attempt_id)
                assert attempt is not None
                await session.delete(attempt)

            with pytest.raises(ExecutionInvariantError):
                await repository.recover_one_expired(backoff_seconds=lambda _: 0)
            async with factory() as session:
                task = await session.get(Task, claim.task_id)
                assert task is not None
                assert task.status == TaskStatus.RUNNING
                assert task.lease_token_hash == claim.lease_token_hash
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_expired_lease_preserves_attempt_and_stale_worker_updates_neither_row(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(postgres_database_url, key="worker-stale")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            factory = create_session_factory(engine)
            repository = WorkerRepository(factory)
            first = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="worker-a",
                lease_duration_seconds=1,
            )
            assert first is not None
            await _expire_lease(postgres_database_url, first.task_id)
            recovery = await repository.recover_one_expired(backoff_seconds=lambda _: 0)
            assert recovery is not None
            second = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="worker-b",
                lease_duration_seconds=2,
            )
            assert second is not None
            assert second.attempt_number == 2

            assert await repository.finalize_success(first, {"stale": True}) == FenceResult.STALE
            attempts = await _load_attempts(postgres_database_url, approved["id"])
            assert [attempt.status for attempt in attempts] == [
                AttemptStatus.LEASE_EXPIRED,
                AttemptStatus.RUNNING,
            ]
            assert await repository.finalize_success(second, {"fresh": True}) == FenceResult.APPLIED
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retry_is_not_claimable_before_available_at_then_succeeds(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(postgres_database_url, key="worker-retry")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            repository = WorkerRepository(create_session_factory(engine))
            first = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="retry-a",
                lease_duration_seconds=2,
            )
            assert first is not None
            assert (
                await repository.schedule_retry(
                    first,
                    delay_seconds=0.15,
                    error_code="transient",
                    error_summary="Task execution failed and will be retried",
                    terminal_reason="retryable_error",
                )
                == FenceResult.APPLIED
            )
            assert (
                await repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="retry-too-soon",
                    lease_duration_seconds=2,
                )
                is None
            )
            await asyncio.sleep(0.18)
            second = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="retry-b",
                lease_duration_seconds=2,
            )
            assert second is not None and second.attempt_number == 2
            assert await repository.finalize_success(second, {"ok": True}) == FenceResult.APPLIED
            persisted = await _load_task(postgres_database_url, approved["id"])
            assert persisted.status == TaskStatus.SUCCEEDED
            assert persisted.result == {"ok": True}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_permanent_failure_and_max_attempts_do_not_retry(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    permanent = _create_and_approve(postgres_database_url, key="worker-permanent")
    maximum = _create_and_approve(
        postgres_database_url,
        key="worker-max-attempt",
        settings=_settings(
            postgres_database_url,
            worker_default_max_attempts=1,
        ),
    )

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            repository = WorkerRepository(create_session_factory(engine))
            first = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="permanent-worker",
                lease_duration_seconds=2,
            )
            assert first is not None
            assert (
                await repository.finalize_failure(
                    first,
                    error_code="permanent",
                    error_summary="Task execution failed permanently",
                    terminal_reason="permanent_error",
                )
                == FenceResult.APPLIED
            )
            second = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="max-worker",
                lease_duration_seconds=2,
            )
            assert second is not None and second.max_attempts == 1
            assert (
                await repository.finalize_failure(
                    second,
                    error_code="retryable",
                    error_summary="Maximum attempts exhausted",
                    terminal_reason="max_attempts_exhausted",
                )
                == FenceResult.APPLIED
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    assert (
        asyncio.run(_load_task(postgres_database_url, permanent["id"])).status == TaskStatus.FAILED
    )
    assert asyncio.run(_load_task(postgres_database_url, maximum["id"])).status == TaskStatus.FAILED


def test_cancellation_before_claim_and_running_cancellation(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    before = _create_and_approve(postgres_database_url, key="worker-cancel-before")
    running = _create_and_approve(postgres_database_url, key="worker-cancel-running")
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        immediate = client.post(f"/tasks/{before['id']}/cancel")
    assert immediate.status_code == 200

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            repository = WorkerRepository(create_session_factory(engine))
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="cancel-worker",
                lease_duration_seconds=2,
            )
            assert claim is not None and str(claim.task_id) == running["id"]
            with TestClient(create_app(_settings(postgres_database_url))) as client:
                requested = client.post(f"/tasks/{running['id']}/cancel")
                repeated = client.post(f"/tasks/{running['id']}/cancel")
            assert requested.status_code == 202
            assert repeated.status_code == 202
            assert repeated.json()["updated_at"] == requested.json()["updated_at"]
            assert (
                await repository.heartbeat(claim, lease_duration_seconds=2)
                == HeartbeatResult.CANCELLATION_REQUESTED
            )
            assert await repository.finalize_cancellation(claim) == FenceResult.APPLIED
            assert (
                await repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="cancel-next",
                    lease_duration_seconds=2,
                )
                is None
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cancellation_plus_worker_crash_does_not_restart_work(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(postgres_database_url, key="worker-cancel-crash")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            repository = WorkerRepository(create_session_factory(engine))
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="crashed-worker",
                lease_duration_seconds=1,
            )
            assert claim is not None
            with TestClient(create_app(_settings(postgres_database_url))) as client:
                assert client.post(f"/tasks/{approved['id']}/cancel").status_code == 202
            await _expire_lease(postgres_database_url, claim.task_id)
            recovered = await repository.recover_one_expired(backoff_seconds=lambda _: 0)
            assert recovered is not None
            assert recovered.new_status == "cancelled"
            assert (
                await repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="replacement",
                    lease_duration_seconds=1,
                )
                is None
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_running_cancellation_is_cooperatively_observed_by_worker(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(
        postgres_database_url,
        key="worker-cooperative-cancellation",
        task_input={"mode": "wait_for_cancellation", "checkpoint_seconds": 0.02},
    )

    async def scenario() -> None:
        settings = _settings(postgres_database_url)
        engine = create_database_engine(settings)
        try:
            repository = WorkerRepository(create_session_factory(engine))
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="cooperative-worker",
                lease_duration_seconds=1,
            )
            assert claim is not None
            registry = HandlerRegistry()
            registry.register("diagnostic", DiagnosticHandler())
            runner_task = asyncio.create_task(
                WorkerRunner(
                    settings=settings,
                    repository=repository,
                    registry=registry,
                    worker_id="cooperative-worker",
                )._execute_claim(claim)
            )
            await asyncio.sleep(0.02)

            def request_cancellation() -> int:
                with TestClient(create_app(settings)) as client:
                    return client.post(f"/tasks/{approved['id']}/cancel").status_code

            assert await asyncio.to_thread(request_cancellation) == 202
            await asyncio.wait_for(runner_task, timeout=1)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    persisted = asyncio.run(_load_task(postgres_database_url, approved["id"]))
    attempts = asyncio.run(_load_attempts(postgres_database_url, approved["id"]))
    assert persisted.status == TaskStatus.CANCELLED
    assert len(attempts) == 1
    assert attempts[0].status == AttemptStatus.CANCELLED
    assert attempts[0].terminal_reason == "customer_cancellation"


def test_concurrent_claim_and_recovery_keep_attempt_numbers_unique(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(postgres_database_url, key="worker-attempt-unique")

    async def scenario() -> None:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            repository = WorkerRepository(create_session_factory(engine))
            first = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="unique-a",
                lease_duration_seconds=1,
            )
            assert first is not None
            await _expire_lease(postgres_database_url, first.task_id)
            await asyncio.gather(
                repository.recover_one_expired(backoff_seconds=lambda _: 0),
                repository.claim_one(
                    supported_task_types=("diagnostic",),
                    worker_id="racing-claim",
                    lease_duration_seconds=1,
                ),
            )
            second = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="unique-b",
                lease_duration_seconds=1,
            )
            if second is None:
                # The racing claim won after recovery.
                attempts = await _load_attempts(postgres_database_url, approved["id"])
            else:
                attempts = await _load_attempts(postgres_database_url, approved["id"])
            assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_diagnostic_retry_then_success_persists_across_runner_instances(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    approved = _create_and_approve(
        postgres_database_url,
        key="worker-runner-retry",
        task_input={"mode": "retry_then_success", "failures": 1},
    )

    async def scenario() -> None:
        settings = _settings(postgres_database_url)
        engine = create_database_engine(settings)
        try:
            repository = WorkerRepository(create_session_factory(engine))
            registry = HandlerRegistry()
            registry.register("diagnostic", DiagnosticHandler())
            first = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="runner-a",
                lease_duration_seconds=1,
            )
            assert first is not None
            await WorkerRunner(
                settings=settings,
                repository=repository,
                registry=registry,
                worker_id="runner-a",
                retry_policy=RetryPolicy(0.01, 0.01, lambda: 0),
            )._execute_claim(first)
            await asyncio.sleep(0.02)
            second = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="runner-b",
                lease_duration_seconds=1,
            )
            assert second is not None
            await WorkerRunner(
                settings=settings,
                repository=repository,
                registry=registry,
                worker_id="runner-b",
            )._execute_claim(second)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        response = client.get(f"/tasks/{approved['id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["attempt_count"] == 2
    assert response.json()["result"]["diagnostic"] == "retry_succeeded"


def test_handler_timeout_persists_sanitized_failure_at_max_attempts(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    settings = _settings(
        postgres_database_url,
        worker_default_max_attempts=1,
        worker_handler_timeout_seconds=0.1,
    )
    approved = _create_and_approve(
        postgres_database_url,
        key="worker-timeout-failure",
        task_input={"mode": "timeout", "duration_seconds": 0.5},
        settings=settings,
    )

    async def scenario() -> None:
        engine = create_database_engine(settings)
        try:
            repository = WorkerRepository(create_session_factory(engine))
            claim = await repository.claim_one(
                supported_task_types=("diagnostic",),
                worker_id="timeout-worker",
                lease_duration_seconds=1,
            )
            assert claim is not None
            registry = HandlerRegistry()
            registry.register("diagnostic", DiagnosticHandler())
            await WorkerRunner(
                settings=settings,
                repository=repository,
                registry=registry,
                worker_id="timeout-worker",
            )._execute_claim(claim)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    persisted = asyncio.run(_load_task(postgres_database_url, approved["id"]))
    attempts = asyncio.run(_load_attempts(postgres_database_url, approved["id"]))
    assert persisted.status == TaskStatus.FAILED
    assert persisted.result is None
    assert persisted.last_error_code == "execution_timeout"
    assert persisted.last_error_summary == "Task execution exceeded its time limit"
    assert len(attempts) == 1
    assert attempts[0].status == AttemptStatus.FAILED
    assert attempts[0].terminal_reason == "max_attempts_exhausted"


def test_graceful_shutdown_stops_new_claims_and_leaves_active_lease(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    first = _create_and_approve(
        postgres_database_url,
        key="worker-shutdown-first",
        task_input={"mode": "delay", "duration_seconds": 1.0},
    )
    second = _create_and_approve(
        postgres_database_url,
        key="worker-shutdown-second",
        task_input={"mode": "delay", "duration_seconds": 1.0},
    )

    async def scenario() -> None:
        settings = _settings(
            postgres_database_url,
            worker_handler_timeout_seconds=2,
            worker_shutdown_grace_seconds=0.1,
        )
        engine = create_database_engine(settings)
        try:
            factory = create_session_factory(engine)
            registry = HandlerRegistry()
            registry.register("diagnostic", DiagnosticHandler())
            runner = WorkerRunner(
                settings=settings,
                repository=WorkerRepository(factory),
                registry=registry,
                worker_id="shutdown-worker",
            )
            worker_task = asyncio.create_task(runner.run())
            for _ in range(100):
                async with factory() as session:
                    running_id = (
                        await session.execute(
                            select(Task.id).where(Task.status == TaskStatus.RUNNING)
                        )
                    ).scalar_one_or_none()
                if running_id is not None:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("worker did not claim the first task")

            runner.request_stop()
            await asyncio.wait_for(worker_task, timeout=1)

            async with factory() as session:
                tasks = list(
                    (
                        await session.execute(
                            select(Task).where(Task.id.in_((UUID(first["id"]), UUID(second["id"]))))
                        )
                    ).scalars()
                )
                attempts = list((await session.execute(select(TaskAttempt))).scalars())
            assert {task.status for task in tasks} == {
                TaskStatus.APPROVED,
                TaskStatus.RUNNING,
            }
            assert len(attempts) == 1
            assert attempts[0].status == AttemptStatus.RUNNING
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_database_execution_constraints_are_installed(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    async def scenario() -> dict[str, str]:
        engine = create_database_engine(_settings(postgres_database_url))
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT constraint_definition.conname,
                               pg_get_constraintdef(constraint_definition.oid)
                        FROM pg_constraint AS constraint_definition
                        JOIN pg_class AS task_table
                          ON task_table.oid = constraint_definition.conrelid
                        JOIN pg_namespace AS task_schema
                          ON task_schema.oid = task_table.relnamespace
                        WHERE task_schema.nspname = current_schema()
                          AND task_table.relname IN ('tasks', 'task_attempts')
                        """
                    )
                )
                return {str(name): str(definition) for name, definition in result.tuples().all()}
        finally:
            await engine.dispose()

    definitions = asyncio.run(scenario())
    for constraint_name in (
        "ck_tasks_attempt_count_nonnegative",
        "ck_tasks_max_attempts_positive",
        "ck_tasks_attempt_count_within_max",
        "ck_tasks_approved_available",
        "ck_tasks_active_lease",
        "ck_tasks_result_state",
        "ck_tasks_cancellation_state",
        "uq_task_attempts_task_number",
    ):
        assert constraint_name in definitions
