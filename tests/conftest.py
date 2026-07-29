from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import delete

from ninjatech_deployment_lab.code_proposals.model import (
    AgentRun,
    AgentStep,
    AgentStepSource,
)
from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import create_database_engine
from ninjatech_deployment_lab.integrations.model import (
    ExternalAction,
    ExternalActionAttempt,
    SourceArtifact,
)
from ninjatech_deployment_lab.tasks.model import Task, TaskAttempt

os.environ.setdefault(
    "NINJATECH_DATABASE_URL",
    "postgresql+asyncpg://ninjatech:test-only@127.0.0.1:5432/ninjatech_test",
)
os.environ.setdefault("NINJATECH_ENVIRONMENT", "test")


@pytest.fixture
def postgres_database_url() -> str:
    """Return the live PostgreSQL test URL or skip database integration tests."""
    database_url = os.getenv("NINJATECH_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("NINJATECH_TEST_DATABASE_URL is not configured")
    return database_url


async def _clear_tasks(database_url: str) -> None:
    engine = create_database_engine(Settings(database_url=database_url, environment="test"))
    try:
        async with engine.begin() as connection:
            await connection.execute(delete(AgentStepSource))
            await connection.execute(delete(AgentStep))
            await connection.execute(delete(AgentRun))
            await connection.execute(delete(ExternalActionAttempt))
            await connection.execute(delete(ExternalAction))
            await connection.execute(delete(SourceArtifact))
            await connection.execute(delete(TaskAttempt))
            await connection.execute(delete(Task))
    finally:
        await engine.dispose()


@pytest.fixture
def clean_tasks(postgres_database_url: str) -> Iterator[None]:
    """Give each PostgreSQL task test an empty table and clean it afterward."""
    asyncio.run(_clear_tasks(postgres_database_url))
    yield
    asyncio.run(_clear_tasks(postgres_database_url))
