from __future__ import annotations

from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.tasks.service import task_request_fingerprint


def test_fingerprint_is_deterministic() -> None:
    task_input: dict[str, JsonValue] = {
        "repository": "example/repository",
        "issue_number": 123,
    }

    assert task_request_fingerprint(
        "code_change",
        task_input,
    ) == task_request_fingerprint("code_change", task_input)


def test_nested_object_key_order_does_not_affect_fingerprint() -> None:
    first: dict[str, JsonValue] = {
        "repository": "example/repository",
        "metadata": {
            "labels": {"priority": "high", "team": "deployment"},
            "issue_number": 123,
        },
    }
    reordered: dict[str, JsonValue] = {
        "metadata": {
            "issue_number": 123,
            "labels": {"team": "deployment", "priority": "high"},
        },
        "repository": "example/repository",
    }

    assert task_request_fingerprint(
        "code_change",
        first,
    ) == task_request_fingerprint("code_change", reordered)


def test_array_order_affects_fingerprint() -> None:
    first: dict[str, JsonValue] = {"steps": ["format", "test", "deploy"]}
    reordered: dict[str, JsonValue] = {"steps": ["test", "format", "deploy"]}

    assert task_request_fingerprint(
        "code_change",
        first,
    ) != task_request_fingerprint("code_change", reordered)


def test_changed_request_content_affects_fingerprint() -> None:
    assert task_request_fingerprint(
        "code_change",
        {"issue_number": 123},
    ) != task_request_fingerprint("code_change", {"issue_number": 124})
