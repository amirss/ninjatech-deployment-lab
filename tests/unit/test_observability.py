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
