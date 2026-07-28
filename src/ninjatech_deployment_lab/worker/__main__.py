from __future__ import annotations

import asyncio
import os
import signal
import socket
from uuid import uuid4

from ninjatech_deployment_lab.config import get_settings
from ninjatech_deployment_lab.database import create_database_engine, create_session_factory
from ninjatech_deployment_lab.integrations.connectors import (
    GitHubClient,
    JiraClient,
    ServiceCatalogClient,
)
from ninjatech_deployment_lab.integrations.credentials import EnvironmentOrFileCredential
from ninjatech_deployment_lab.integrations.http import IntegrationHttpClient
from ninjatech_deployment_lab.integrations.metrics import StructuredLoggingMetricsSink
from ninjatech_deployment_lab.integrations.persistence import (
    ExternalActionRepository,
    SourceArtifactRepository,
)
from ninjatech_deployment_lab.integrations.slack import (
    SlackClient,
    SlackDeliveryService,
)
from ninjatech_deployment_lab.integrations.workflow import DeploymentContextSyncHandler
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
    integration_http: IntegrationHttpClient | None = None
    if settings.enable_diagnostic_handler:
        registry.register("diagnostic", DiagnosticHandler())
    if settings.enable_deployment_context_sync:
        metrics = StructuredLoggingMetricsSink()
        integration_http = IntegrationHttpClient(settings, metrics=metrics)
        action_repository = ExternalActionRepository(session_factory, metrics=metrics)
        service_catalog_credential = EnvironmentOrFileCredential(
            value=settings.service_catalog_token,
            path=settings.service_catalog_token_file,
        )
        jira_credential = EnvironmentOrFileCredential(
            value=settings.jira_api_token,
            path=settings.jira_api_token_file,
        )
        github_credential = EnvironmentOrFileCredential(
            value=settings.github_token,
            path=settings.github_token_file,
        )

        def build_slack_delivery() -> SlackDeliveryService:
            if integration_http is None:
                raise RuntimeError("integration HTTP client is not configured")
            slack_credential = EnvironmentOrFileCredential(
                value=settings.slack_bot_token,
                path=settings.slack_bot_token_file,
            )
            return SlackDeliveryService(
                settings=settings,
                notifier=SlackClient(
                    http=integration_http,
                    base_url=settings.slack_base_url,
                    credential=slack_credential,
                    settings=settings,
                ),
                actions=action_repository,
                metrics=metrics,
            )

        registry.register(
            "deployment_context_sync",
            DeploymentContextSyncHandler(
                settings=settings,
                service_catalog=ServiceCatalogClient(
                    http=integration_http,
                    base_url=settings.service_catalog_base_url,
                    credential=service_catalog_credential,
                    settings=settings,
                ),
                jira=JiraClient(
                    http=integration_http,
                    base_url=settings.jira_base_url,
                    credential=jira_credential,
                    settings=settings,
                ),
                github=GitHubClient(
                    http=integration_http,
                    base_url=settings.github_base_url,
                    credential=github_credential,
                    settings=settings,
                ),
                artifacts=SourceArtifactRepository(
                    session_factory,
                    maximum_bytes=settings.source_artifact_max_bytes,
                ),
                actions=action_repository,
                metrics=metrics,
                slack_delivery_factory=(
                    build_slack_delivery if settings.enable_slack_notification else None
                ),
            ),
        )

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
        if integration_http is not None:
            await integration_http.aclose()
        await engine.dispose()


def main() -> None:
    """Synchronous module entry point."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
