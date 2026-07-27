from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import create_database_engine
from ninjatech_deployment_lab.main import create_app
from ninjatech_deployment_lab.tasks.model import Task

pytestmark = pytest.mark.postgres

TASK_PAYLOAD: dict[str, Any] = {
    "task_type": "code_change",
    "input": {
        "repository": "example/repository",
        "issue_number": 123,
    },
}
PUBLIC_TASK_FIELDS = {
    "id",
    "task_type",
    "input",
    "status",
    "attempt_count",
    "max_attempts",
    "available_at",
    "cancellation_requested_at",
    "result",
    "last_error_code",
    "last_error_summary",
    "created_at",
    "updated_at",
}


def _settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, environment="test")


def _task_body(response_json: object) -> dict[str, Any]:
    return cast(dict[str, Any], response_json)


async def _count_tasks_for_key(database_url: str, idempotency_key: str) -> int:
    engine = create_database_engine(_settings(database_url))
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.idempotency_key == idempotency_key)
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


def _concurrent_posts(
    database_url: str,
    idempotency_key: str,
    payloads: tuple[dict[str, Any], dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    applications = (create_app(_settings(database_url)), create_app(_settings(database_url)))
    barrier = threading.Barrier(2)

    def post(application: FastAPI, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with TestClient(application) as client:
            barrier.wait()
            response = client.post(
                "/tasks",
                headers={"Idempotency-Key": idempotency_key},
                json=payload,
            )
            return response.status_code, _task_body(response.json())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(post, application, payload)
            for application, payload in zip(applications, payloads, strict=True)
        ]
        return [future.result() for future in futures]


def test_create_retrieve_and_response_privacy(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        created = client.post(
            "/tasks",
            headers={"Idempotency-Key": "create-retrieve-key"},
            json=TASK_PAYLOAD,
        )
        assert created.status_code == 201
        created_body = _task_body(created.json())
        assert set(created_body) == PUBLIC_TASK_FIELDS
        assert "idempotency_key" not in created_body
        assert "request_fingerprint" not in created_body

        retrieved = client.get(f"/tasks/{created_body['id']}")

    assert retrieved.status_code == 200
    assert retrieved.json() == created_body


def test_task_not_found(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        response = client.get(f"/tasks/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"


def test_create_replay_preserves_task_and_updated_at(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        first = client.post(
            "/tasks",
            headers={"Idempotency-Key": "create-replay-key"},
            json=TASK_PAYLOAD,
        )
        replay = client.post(
            "/tasks",
            headers={"Idempotency-Key": "create-replay-key"},
            json=TASK_PAYLOAD,
        )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_same_key_different_body_returns_conflict(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        first = client.post(
            "/tasks",
            headers={"Idempotency-Key": "sequential-conflict-key"},
            json=TASK_PAYLOAD,
        )
        conflict = client.post(
            "/tasks",
            headers={"Idempotency-Key": "sequential-conflict-key"},
            json={
                "task_type": "code_change",
                "input": {
                    "repository": "example/repository",
                    "issue_number": 999,
                },
            },
        )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_approve_updates_timestamp_and_repeated_approve_does_not(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        created = client.post(
            "/tasks",
            headers={"Idempotency-Key": "approve-timestamp-key"},
            json=TASK_PAYLOAD,
        )
        approved = client.post(f"/tasks/{created.json()['id']}/approve")
        replay = client.post(f"/tasks/{created.json()['id']}/approve")

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert datetime.fromisoformat(approved.json()["updated_at"]) > datetime.fromisoformat(
        created.json()["updated_at"]
    )
    assert replay.status_code == 200
    assert replay.json()["updated_at"] == approved.json()["updated_at"]


def test_cancel_updates_timestamp_and_repeated_cancel_does_not(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        created = client.post(
            "/tasks",
            headers={"Idempotency-Key": "cancel-timestamp-key"},
            json=TASK_PAYLOAD,
        )
        cancelled = client.post(f"/tasks/{created.json()['id']}/cancel")
        replay = client.post(f"/tasks/{created.json()['id']}/cancel")
        invalid_approval = client.post(f"/tasks/{created.json()['id']}/approve")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert datetime.fromisoformat(cancelled.json()["updated_at"]) > datetime.fromisoformat(
        created.json()["updated_at"]
    )
    assert replay.status_code == 200
    assert replay.json()["updated_at"] == cancelled.json()["updated_at"]
    assert invalid_approval.status_code == 409
    assert invalid_approval.json()["error"]["code"] == "invalid_task_transition"


def test_same_key_same_body_concurrently_creates_exactly_one_row(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    key = "concurrent-same-body-key"
    results = _concurrent_posts(
        postgres_database_url,
        key,
        (TASK_PAYLOAD, TASK_PAYLOAD),
    )

    assert sorted(status_code for status_code, _ in results) == [200, 201]
    assert len({body["id"] for _, body in results}) == 1
    assert asyncio.run(_count_tasks_for_key(postgres_database_url, key)) == 1


def test_same_key_different_bodies_concurrently_create_one_and_conflict_one(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    key = "concurrent-different-body-key"
    changed_payload = {
        "task_type": "code_change",
        "input": {
            "repository": "example/repository",
            "issue_number": 456,
        },
    }
    results = _concurrent_posts(
        postgres_database_url,
        key,
        (TASK_PAYLOAD, changed_payload),
    )

    assert sorted(status_code for status_code, _ in results) == [201, 409]
    assert asyncio.run(_count_tasks_for_key(postgres_database_url, key)) == 1


def test_task_persists_across_separate_application_sessions(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as first_client:
        created = first_client.post(
            "/tasks",
            headers={"Idempotency-Key": "separate-session-key"},
            json=TASK_PAYLOAD,
        )
    with TestClient(create_app(_settings(postgres_database_url))) as second_client:
        retrieved = second_client.get(f"/tasks/{created.json()['id']}")

    assert created.status_code == 201
    assert retrieved.status_code == 200
    assert retrieved.json() == created.json()


def test_concurrent_approve_and_cancel_serialize_without_lost_update(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    with TestClient(create_app(_settings(postgres_database_url))) as client:
        created = client.post(
            "/tasks",
            headers={"Idempotency-Key": "approve-cancel-race-key"},
            json=TASK_PAYLOAD,
        )
    task_id = created.json()["id"]
    applications = (
        create_app(_settings(postgres_database_url)),
        create_app(_settings(postgres_database_url)),
    )
    barrier = threading.Barrier(2)

    def transition(application: FastAPI, operation: str) -> tuple[int, dict[str, Any]]:
        with TestClient(application) as client:
            barrier.wait()
            response = client.post(f"/tasks/{task_id}/{operation}")
            return response.status_code, _task_body(response.json())

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve_future = executor.submit(transition, applications[0], "approve")
        cancel_future = executor.submit(transition, applications[1], "cancel")
        approve_result = approve_future.result()
        cancel_result = cancel_future.result()

    with TestClient(create_app(_settings(postgres_database_url))) as client:
        final_task = client.get(f"/tasks/{task_id}")

    assert approve_result[0] in {200, 409}
    assert cancel_result[0] == 200
    assert final_task.status_code == 200
    assert final_task.json()["status"] == "cancelled"
    assert asyncio.run(_count_tasks_for_key(postgres_database_url, "approve-cancel-race-key")) == 1


async def _task_constraint_definitions(database_url: str) -> dict[str, str]:
    engine = create_database_engine(_settings(database_url))
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
                      AND task_table.relname = 'tasks'
                    """
                )
            )
            return {str(name): str(definition) for name, definition in result.tuples().all()}
    finally:
        await engine.dispose()


def test_database_installs_idempotency_and_status_constraints(
    postgres_database_url: str,
    clean_tasks: None,
) -> None:
    definitions = asyncio.run(_task_constraint_definitions(postgres_database_url))

    assert definitions["uq_tasks_idempotency_key"] == "UNIQUE (idempotency_key)"
    status_definition = definitions["ck_tasks_status"]
    for status in (
        "pending_approval",
        "approved",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ):
        assert status in status_definition
