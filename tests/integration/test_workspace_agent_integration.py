from __future__ import annotations

import json
from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.database import Database
from antigona.models import FlowStep, StepState, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.worker.agent_core import AgentCoreConfig, create_worker_agent_core
from antigona.workspace import BaseWorkspace, WorkspaceFactory


def test_agent_core_runs_through_base_workspace_local(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    ws_dir = tmp_path / "workspace"
    persistence = tmp_path / "persistence"

    ws = WorkspaceFactory.create_workspace(backend="local", workspace_dir=ws_dir)
    assert isinstance(ws, BaseWorkspace)

    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(CreateTask("owner", "write", "out.txt", "content", "idem-local"))
        repo.transition(task, TaskState.QUEUED, "queued", "test", correlation_id="c1")
        repo.commit()
        step = task.steps[0]

        core = create_worker_agent_core(
            AgentCoreConfig(
                workspace=ws,
                persistence_dir=persistence,
                conversation_id="conv-local",
            ),
            repo,
            force_scripted=True,
        )

        prompt = json.dumps({
            "tool": "workspace.write_text",
            "arguments": {"path": "out.txt", "content": "hello base workspace"},
        })

        core.run_turn(task=task, step=step, prompt=prompt, correlation_id="c1")
        refreshed = repo.get(task.id)

        assert (ws_dir / "out.txt").read_text(encoding="utf-8") == "hello base workspace"
        assert len(refreshed.artifacts) == 1
        assert refreshed.artifacts[0].path == "out.txt"


@pytest.mark.parametrize("backend_name", ["ssh", "modal", "daytona"])
def test_agent_core_runs_through_mock_remote_backend(tmp_path: Path, backend_name: str) -> None:
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    persistence = tmp_path / "persistence"

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
        workspace=tmp_path / backend_name,
        workspace_backend=backend_name,
        workspace_mock=True,
    )

    ws = WorkspaceFactory.create_workspace(config=settings)

    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(CreateTask("owner", "write remote", "remote.txt", "data", f"idem-{backend_name}"))
        step2 = FlowStep(
            task_id=task.id,
            index=1,
            title="Step 2: shell execution",
            input={"tool_name": "sandbox.shell"},
            status=StepState.PENDING.value,
        )
        task.steps.append(step2)
        repo.transition(task, TaskState.QUEUED, "queued", "test", correlation_id="c2")
        repo.commit()
        step1 = task.steps[0]

        core = create_worker_agent_core(
            AgentCoreConfig(
                workspace=ws,
                persistence_dir=persistence,
                conversation_id=f"conv-{backend_name}",
            ),
            repo,
            force_scripted=True,
        )

        # Write text in turn 1
        write_prompt = json.dumps({
            "tool": "workspace.write_text",
            "arguments": {"path": "remote.txt", "content": f"hello {backend_name}"},
        })
        core.run_turn(task=task, step=step1, prompt=write_prompt, correlation_id="c2")

        # Execute shell command in turn 2
        shell_prompt = json.dumps({
            "tool": "sandbox.shell",
            "arguments": {"command": ["echo", "hi"]},
        })
        core.run_turn(task=task, step=step2, prompt=shell_prompt, correlation_id="c3")

        refreshed = repo.get(task.id)
        assert len(refreshed.artifacts) >= 1
        assert refreshed.artifacts[0].path == "remote.txt"


def test_per_task_workspace_isolation(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    persistence = tmp_path / "persistence"

    ws1 = WorkspaceFactory.create_workspace(backend="local", workspace_dir=tmp_path, task_id="task-111")
    ws2 = WorkspaceFactory.create_workspace(backend="local", workspace_dir=tmp_path, task_id="task-222")

    with db.session_factory() as session:
        repo = TaskRepository(session)
        t1, _ = repo.create(CreateTask("o1", "g1", "file.txt", "c1", "idem-t1"))
        t2, _ = repo.create(CreateTask("o2", "g2", "file.txt", "c2", "idem-t2"))
        repo.commit()

        core1 = create_worker_agent_core(
            AgentCoreConfig(workspace=ws1, persistence_dir=persistence, conversation_id="conv-t1"),
            repo,
            force_scripted=True,
        )
        core2 = create_worker_agent_core(
            AgentCoreConfig(workspace=ws2, persistence_dir=persistence, conversation_id="conv-t2"),
            repo,
            force_scripted=True,
        )

        core1.run_turn(
            task=t1,
            step=t1.steps[0],
            prompt=json.dumps({"tool": "workspace.write_text", "arguments": {"path": "f.txt", "content": "data1"}}),
            correlation_id="c1",
        )
        core2.run_turn(
            task=t2,
            step=t2.steps[0],
            prompt=json.dumps({"tool": "workspace.write_text", "arguments": {"path": "f.txt", "content": "data2"}}),
            correlation_id="c2",
        )

    assert (ws1.root_path / "f.txt").read_text(encoding="utf-8") == "data1"
    assert (ws2.root_path / "f.txt").read_text(encoding="utf-8") == "data2"
