from __future__ import annotations

import asyncio
import logging
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ninjatech_deployment_lab.config import Settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative metadata root for future application models."""


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Create the application's lazy asynchronous database engine."""
    return create_async_engine(
        settings.database_url,
        hide_parameters=True,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create request-scoped sessions that retain loaded values after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def is_database_ready(engine: AsyncEngine, timeout_seconds: float) -> bool:
    """Return true only when PostgreSQL answers a minimal query before the deadline."""
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT 1"))
                return cast(int, result.scalar_one()) == 1
    except Exception:
        logger.exception("database_readiness_check_failed")
        return False
