from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from antigona.database import Database
from antigona.observability import event
from antigona.repository import CreateTask, TaskRepository
from antigona.worker.agent_core import ScriptedConversation


def test_json_event_has_utc_timestamp_service_and_trace_context(caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="antigona")

    event(
        "worker.tool_result",
        service="worker",
        correlation_id="corr-1",
        task_id="task-1",
        session_id="session-1",
        step_id="step-1",
        status="completed",
        trust="untrusted",
    )

    record = json.loads(caplog.records[-1].message)
    assert record["timestamp"].endswith("Z")
    assert record["service"] == "worker"
    assert record["correlation_id"] == "corr-1"
    assert record["task_id"] == "task-1"
    assert record["session_id"] == "session-1"
    assert record["step_id"] == "step-1"
    assert record["trust"] == "untrusted"


def test_scripted_conversation_persists_bounded_summary_next_to_state(tmp_path: Path) -> None:
    conversation = ScriptedConversation(tmp_path, "conversation-1")
    conversation.register_tool(
        "workspace.read_text",
        lambda arguments: {
            "path": arguments["path"],
            "content": "x" * 10_000,
            "untrusted": True,
        },
    )

    conversation.run(
        json.dumps(
            {
                "tool": "workspace.read_text",
                "arguments": {"path": "input.txt", "untrusted": True},
            }
        ),
        callbacks=[],
    )

    summary_path = tmp_path / "conversation-1.summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary == {
        "conversation_id": "conversation-1",
        "last_tool": "workspace.read_text",
        "last_trust": "untrusted",
        "turn_count": 1,
    }
    assert summary_path.stat().st_size < 512


def test_state_transition_audit_is_database_append_only(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'audit.db'}")
    database.create_all()
    with database.session_factory() as session:
        task, _ = TaskRepository(session).create(
            CreateTask("owner", "goal", "proof.txt", "ok", "idem")
        )
        transition_id = task.transitions[0].id

        with pytest.raises(DatabaseError, match="state_transitions is append-only"):
            session.execute(
                text("UPDATE state_transitions SET reason='tampered' WHERE id=:id"),
                {"id": transition_id},
            )
            session.commit()
        session.rollback()

        with pytest.raises(DatabaseError, match="state_transitions is append-only"):
            session.execute(
                text("DELETE FROM state_transitions WHERE id=:id"),
                {"id": transition_id},
            )
            session.commit()
