from __future__ import annotations

import asyncio
import os
import signal
import socket
from uuid import uuid4

from ninjatech_deployment_lab.config import get_settings
from ninjatech_deployment_lab.database import create_database_engine, create_session_factory
from ninjatech_deployment_lab.observability import configure_logging
from ninjatech_deployment_lab.worker.diagnostic import DiagnosticHandler
from ninjatech_deployment_lab.worker.handlers import HandlerRegistry
from ninjatech_deployment_lab.worker.repository import WorkerRepository
from ninjatech_deployment_lab.worker.runner import WorkerRunner


async def async_main() -> None:
    """Build and run one worker process until a termination signal."""
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=f"{settings.app_name} Worker",
    )
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    registry = HandlerRegistry()
    if settings.enable_diagnostic_handler:
        registry.register("diagnostic", DiagnosticHandler())

    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
    runner = WorkerRunner(
        settings=settings,
        repository=WorkerRepository(session_factory),
        registry=registry,
        worker_id=worker_id,
    )
    loop = asyncio.get_running_loop()
    for termination_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(termination_signal, runner.request_stop)
    try:
        await runner.run()
    finally:
        await engine.dispose()


def main() -> None:
    """Synchronous module entry point."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
