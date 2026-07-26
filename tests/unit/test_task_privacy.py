from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import StatementError

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import create_database_engine
from ninjatech_deployment_lab.tasks.domain import TaskStatus
from ninjatech_deployment_lab.tasks.model import Task
from ninjatech_deployment_lab.tasks.schemas import TaskResponse
from ninjatech_deployment_lab.tasks.service import log_task_event


def _task_with_sensitive_internal_data() -> Task:
    timestamp = datetime.now(UTC)
    return Task(
        id=uuid4(),
        idempotency_key="raw-idempotency-key-never-log",
        request_fingerprint="a" * 64,
        task_type="code_change",
        task_input={
            "repository": "private/repository-never-log",
            "credential": "complete-task-input-secret",
        },
        status=TaskStatus.PENDING_APPROVAL,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_task_response_excludes_internal_idempotency_fields() -> None:
    response = TaskResponse.model_validate(_task_with_sensitive_internal_data())
    payload = response.model_dump(mode="json")

    assert "idempotency_key" not in payload
    assert "request_fingerprint" not in payload


def test_lifecycle_logs_exclude_raw_key_and_complete_input(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = _task_with_sensitive_internal_data()
    caplog.set_level("INFO", logger="ninjatech_deployment_lab.tasks.service")

    log_task_event(
        event="task_created",
        task=task,
        previous_status=None,
        new_status=TaskStatus.PENDING_APPROVAL,
        idempotency_key=task.idempotency_key,
    )

    captured = json.dumps(
        [record.__dict__ for record in caplog.records],
        default=str,
        sort_keys=True,
    )
    assert task.idempotency_key not in captured
    assert "private/repository-never-log" not in captured
    assert "complete-task-input-secret" not in captured


def test_sqlalchemy_errors_hide_bound_parameters() -> None:
    secret_parameter = "bound-parameter-never-print"
    engine = create_database_engine(
        Settings(
            database_url="postgresql+asyncpg://user:password@127.0.0.1:5432/test",
            environment="test",
        )
    )
    try:
        assert engine.sync_engine.hide_parameters is True
    finally:
        asyncio.run(engine.dispose())

    error = StatementError(
        "database operation failed",
        "INSERT INTO tasks (task_input) VALUES (:task_input)",
        {"task_input": secret_parameter},
        RuntimeError("driver failure"),
        hide_parameters=True,
    )
    rendered_error = str(error)

    assert secret_parameter not in rendered_error
    assert "SQL parameters hidden" in rendered_error
