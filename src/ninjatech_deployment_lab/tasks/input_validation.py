from __future__ import annotations

from ninjatech_deployment_lab.integrations.domain import DeploymentContextSyncInput
from ninjatech_deployment_lab.tasks.schemas import JsonValue


def normalize_task_input(
    task_type: str,
    task_input: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Validate known task contracts before persistence or worker execution."""
    if task_type != "deployment_context_sync":
        return task_input
    validated = DeploymentContextSyncInput.model_validate(task_input)
    return validated.model_dump(mode="json", exclude_none=True)
