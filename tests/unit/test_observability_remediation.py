from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from antigona.database import Database
from antigona.observability import EventEnvelope, event, flow_event, redact


@pytest.mark.parametrize(
    "value",
    [
        "Authorization: Bearer synthetic-bearer-value",
        "authorization=Bearer synthetic-equals-value",
        "request https://synthetic-user:synthetic-password@example.invalid/path",
        {
            "outer": [
                {"api_key": "synthetic-api-value"},
                {"access-token": "synthetic-access-value"},
                {"dbPassword": "synthetic-db-value"},
                "token: synthetic-inline-value",
            ]
        },
    ],
)
def test_recursive_redaction_removes_complete_synthetic_secrets(value: object) -> None:
    rendered = json.dumps(redact(value), sort_keys=True)
    for secret in (
        "synthetic-bearer-value",
        "synthetic-equals-value",
        "synthetic-user",
        "synthetic-password",
        "synthetic-api-value",
        "synthetic-access-value",
        "synthetic-db-value",
        "synthetic-inline-value",
    ):
        assert secret not in rendered


def test_redaction_supports_nested_generic_mappings() -> None:
    class CustomMapping(Mapping[str, Any]):
        def __init__(self) -> None:
            self._data = {"client_secret": "synthetic-client-value"}

        def __getitem__(self, key: str) -> Any:
            return self._data[key]

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

    assert redact({"nested": CustomMapping()}) == {"nested": {"client_secret": "[REDACTED]"}}


@pytest.mark.parametrize(
    ("value", "secrets"),
    [
        ('Authorization: Bearer "synthetic quoted secret"', ("synthetic", "quoted", "secret")),
        ('Authorization=Bearer "synthetic escaped \\"quote\\" secret"', ("synthetic escaped",)),
        ("https://synthetic-user:pa%40ss@example.invalid/x", ("synthetic-user", "pa%40ss")),
        ("https://synthetic-user:pa@ss@example.invalid/x", ("synthetic-user", "pa@ss")),
        ("nested https://synthetic-u:p%3A%2F%2F@host.invalid/a", ("synthetic-u", "p%3A%2F%2F")),
    ],
)
def test_adversarial_redaction_removes_complete_credentials(
    value: str, secrets: tuple[str, ...]
) -> None:
    rendered = json.dumps(redact({"outer": [{"message": value}]}))
    assert "[REDACTED]" in rendered
    assert all(secret not in rendered for secret in secrets)


def _records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(record.message) for record in caplog.records if record.name == "antigona"]


def test_database_hooks_emit_duration_slow_and_error_without_sql_or_parameters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="antigona")
    database = Database("sqlite:///:memory:", slow_query_threshold_ms=0)

    with database.engine.connect() as connection:
        connection.execute(text("SELECT :synthetic_parameter"), {"synthetic_parameter": "synthetic-db-param"})
        with pytest.raises(DBAPIError):
            connection.exec_driver_sql("SELECT * FROM missing_synthetic_table")

    records = _records(caplog)
    completed = [record for record in records if record["event"] == "database.query.completed"]
    failed = [record for record in records if record["event"] == "database.query.failed"]
    assert completed and failed
    assert any(record["status"] == "slow" for record in completed)
    assert all(record["service"] == "database" for record in completed + failed)
    assert all(isinstance(record["duration_ms"], (int, float)) for record in completed + failed)
    rendered = json.dumps(records)
    assert "synthetic-db-param" not in rendered
    assert "missing_synthetic_table" not in rendered
    assert "statement" not in rendered
    assert "parameters" not in rendered


def test_event_contract_rejects_missing_required_service_and_correlation() -> None:
    with pytest.raises(ValueError, match="service"):
        event("worker.invalid", correlation_id="corr")
    with pytest.raises(ValueError, match="correlation_id"):
        event("worker.invalid", service="worker")
    with pytest.raises(ValueError, match="task_id"):
        event("worker.invalid", service="worker", correlation_id="corr")


def test_typed_flow_event_always_emits_complete_nullable_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="antigona")
    flow_event(
        "gateway.authentication_failed",
        EventEnvelope("gateway", "corr", None, None, None, "401"),
        reason="authentication_denied",
    )
    record = _records(caplog)[-1]
    assert {"timestamp", "event", "service", "correlation_id", "task_id", "session_id", "step_id", "status"} <= record.keys()
