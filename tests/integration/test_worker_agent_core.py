from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from antigona.database import Database
from antigona.models import TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.worker.agent_core import (
    AgentCoreConfig,
    ScriptedConversation,
    create_worker_agent_core,
)
from antigona.worker.tools.common import ToolError


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX st_mode bits (0o600/0o640/0o750) not enforceable on Windows (Wave 4)')
def test_agent_core_writes_file_and_projects_events(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path/'db.sqlite'}")
    db.create_all()
    workspace = tmp_path / "workspace"
    persistence = tmp_path / "persistence"
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(CreateTask("owner", "write", "unused.txt", "ignored", "idem-1"))
        repo.transition(task, TaskState.QUEUED, "queued for worker", "test", correlation_id="corr-1")
        repo.commit()
        step = task.steps[0]
        core = create_worker_agent_core(
            AgentCoreConfig(
                workspace=workspace,
                persistence_dir=persistence,
                conversation_id="conv-1",
            ),
            repo,
            force_scripted=True,
        )
        prompt = json.dumps(
            {
                "tool": "workspace.write_text",
                "arguments": {"path": "notes/hello.txt", "content": "antigona"},
            }
        )
        core.run_turn(task=task, step=step, prompt=prompt, correlation_id="corr-1")
        refreshed = repo.get(task.id)
        assert (workspace / "notes" / "hello.txt").read_text(encoding="utf-8") == "antigona"
        assert (workspace.stat().st_mode & 0o777) == 0o750
        assert refreshed.status == TaskState.VERIFYING.value
        assert len(refreshed.transitions) >= 6


def test_scripted_conversation_restores_from_persistence(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path/'db.sqlite'}")
    db.create_all()
    workspace = tmp_path / "workspace"
    persistence = tmp_path / "persistence"
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(CreateTask("owner", "write", "unused.txt", "ignored", "idem-2"))
        repo.transition(task, TaskState.QUEUED, "queued for worker", "test", correlation_id="corr-2")
        repo.commit()
        core_a = create_worker_agent_core(
            AgentCoreConfig(
                workspace=workspace,
                persistence_dir=persistence,
                conversation_id="conv-restore",
            ),
            repo,
            force_scripted=True,
        )
        assert isinstance(core_a.conversation, ScriptedConversation)
        step = task.steps[0]
        prompt = json.dumps(
            {
                "tool": "workspace.write_text",
                "arguments": {"path": "restore/file.txt", "content": "one"},
            }
        )
        core_a.run_turn(task=task, step=step, prompt=prompt, correlation_id="corr-2")

    with db.session_factory() as session:
        repo = TaskRepository(session)
        core_b = create_worker_agent_core(
            AgentCoreConfig(
                workspace=workspace,
                persistence_dir=persistence,
                conversation_id="conv-restore",
            ),
            repo,
            force_scripted=True,
        )
        assert isinstance(core_b.conversation, ScriptedConversation)
        assert len(core_b.conversation.turns) == 1


def test_untrusted_context_is_durable_sticky_and_task_isolated(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path/'db.sqlite'}")
    db.create_all()
    workspace = tmp_path / "workspace"
    persistence = tmp_path / "persistence"
    workspace.mkdir()
    (workspace / "input.txt").write_text("input", encoding="utf-8")
    config = AgentCoreConfig(workspace, persistence, "conv-trust")
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(CreateTask("owner", "read", "unused.txt", "ignored", "trust-1"))
        other, _ = repo.create(CreateTask("owner", "read", "unused.txt", "ignored", "trust-2"))
        repo.commit()
        core = create_worker_agent_core(config, repo, force_scripted=True)
        core._bind_trust_state(task)
        core._tool_read_text({"path": "input.txt", "untrusted": True})
        assert core.untrusted_context is True

    with db.session_factory() as session:
        repo = TaskRepository(session)
        restarted = create_worker_agent_core(config, repo, force_scripted=True)
        restarted._bind_trust_state(repo.get(task.id))
        assert restarted.untrusted_context is True
        restarted._tool_read_text({"path": "input.txt", "untrusted": False})
        assert restarted.untrusted_context is True
        with pytest.raises(ToolError, match="disabled after reading untrusted"):
            restarted._tool_shell({"command": ["true"]})

        restarted._bind_trust_state(repo.get(other.id))
        assert restarted.untrusted_context is False
