from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncEngine

from ninjatech_deployment_lab.database import is_database_ready


def test_database_ready_when_select_one_succeeds() -> None:
    result = MagicMock()
    result.scalar_one.return_value = 1

    connection = AsyncMock()
    connection.execute.return_value = result

    connection_context = AsyncMock()
    connection_context.__aenter__.return_value = connection

    engine = MagicMock(spec=AsyncEngine)
    engine.connect.return_value = connection_context

    assert asyncio.run(is_database_ready(engine, timeout_seconds=1.0)) is True


def test_database_not_ready_when_connection_fails() -> None:
    engine = MagicMock(spec=AsyncEngine)
    engine.connect.side_effect = RuntimeError("database is unavailable")

    assert asyncio.run(is_database_ready(engine, timeout_seconds=1.0)) is False
