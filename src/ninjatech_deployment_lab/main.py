from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ninjatech_deployment_lab.api import router
from ninjatech_deployment_lab.config import Settings, get_settings
from ninjatech_deployment_lab.database import create_database_engine
from ninjatech_deployment_lab.observability import (
    RequestContextMiddleware,
    configure_logging,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application instance with explicit configuration and owned resources."""
    application_settings = settings or get_settings()
    configure_logging(
        level=application_settings.log_level,
        service_name=application_settings.app_name,
    )
    engine = create_database_engine(application_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "application_started",
            extra={"environment": application_settings.environment},
        )
        try:
            yield
        finally:
            await engine.dispose()
            logger.info("application_stopped")

    application = FastAPI(
        title=application_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = application_settings
    application.state.database_engine = engine
    application.add_middleware(RequestContextMiddleware)
    application.include_router(router)
    return application


app = create_app()
