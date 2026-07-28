from __future__ import annotations

import json
import logging
import sys

from ninjatech_deployment_lab.observability import JsonFormatter, normalize_request_id


def test_normalize_request_id_preserves_safe_value() -> None:
    assert normalize_request_id("ticket:ABC-123") == "ticket:ABC-123"


def test_normalize_request_id_replaces_unsafe_value() -> None:
    request_id = normalize_request_id("unsafe\nvalue")

    assert len(request_id) == 32
    assert request_id.isalnum()


def test_json_formatter_emits_machine_readable_fields() -> None:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test_message",
        args=(),
        exc_info=None,
    )
    formatter = JsonFormatter(service_name="test-service")

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "test_message"
    assert payload["service"] == "test-service"


def test_json_formatter_never_serializes_exception_messages_or_tracebacks() -> None:
    try:
        raise RuntimeError("exception-secret-never-log")
    except RuntimeError:
        exception_info = sys.exc_info()

    record = logging.LogRecord(
        name="test.logger",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="safe_failure_event",
        args=(),
        exc_info=exception_info,
    )

    rendered = JsonFormatter(service_name="test-service").format(record)

    assert "safe_failure_event" in rendered
    assert "exception-secret-never-log" not in rendered
    assert "Traceback" not in rendered


def test_integration_logs_use_allowlisted_metadata_only() -> None:
    record = logging.LogRecord(
        name="ninjatech_deployment_lab.integrations.workflow",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="external_action_succeeded",
        args=(),
        exc_info=None,
    )
    record.action_id = "action-safe-id"
    record.provider = "github"
    record.task_input = {"description": "complete-customer-payload-never-log"}
    record.authorization = "Bearer provider-secret-never-log"
    record.result = {"comment": "complete-comment-never-log"}
    record.action_scope_key = "customer-business-scope-never-log"
    record.lease_token_hash = "lease-hash-never-log"

    rendered = JsonFormatter(service_name="test-service").format(record)

    assert "action-safe-id" in rendered
    assert "github" in rendered
    for secret in (
        "complete-customer-payload-never-log",
        "provider-secret-never-log",
        "complete-comment-never-log",
        "customer-business-scope-never-log",
        "lease-hash-never-log",
    ):
        assert secret not in rendered


def test_json_formatter_emits_only_low_cardinality_metric_fields() -> None:
    record = logging.LogRecord(
        name="ninjatech_deployment_lab.integrations.metrics",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="operational_metric",
        args=(),
        exc_info=None,
    )
    record.metric_name = "provider_request_count"
    record.metric_kind = "counter"
    record.metric_value = 1.0
    record.metric_labels = {
        "provider": "slack",
        "operation": "write",
        "outcome": "success",
    }
    record.task_id = "task-must-not-be-used-as-a-metric-label"

    payload = json.loads(JsonFormatter(service_name="test-service").format(record))

    assert payload["metric_name"] == "provider_request_count"
    assert payload["metric_labels"] == {
        "provider": "slack",
        "operation": "write",
        "outcome": "success",
    }
    assert "task_id" in payload
    assert "task_id" not in payload["metric_labels"]
