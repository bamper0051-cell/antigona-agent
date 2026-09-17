from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from antigona.config import Settings
from antigona.core.task_service import TaskSubmissionService
from antigona.database import Database
from antigona.gateway.api import create_gateway_app
from antigona.models import Artifact, FlowStep, StateTransition, TaskFlow, TaskState, utcnow
from antigona.repository import TaskRepository

T07_GOAL = (
    "Создай файл с именем manual test.txt в workspace.\n"
    "Содержимое должно быть ровно двумя строками:\n"
    "ANTIGONA FILE TEST\n"
    "12345"
)
T07_CONTENT = "ANTIGONA FILE TEST\n12345"


def test_t07_goal_parser_preserves_multiline_content_before_readback_suffix() -> None:
    from antigona.task_goal import parse_goal

    goal = (
        "Создай файл с именем manual test.txt в workspace.\n"
        "Содержимое должно быть ровно двумя строками:\n"
        "ANTIGONA FILE TEST\n"
        "12345\n"
        "После записи прочитай этот же файл обратно и ответь полным отчётом: "
        "путь, tool write, tool read, точное содержимое."
    )

    plan = parse_goal(goal)

    assert plan.intent == "file_write_read"
    assert plan.path == "manual test.txt"
    assert plan.content == "ANTIGONA FILE TEST\n12345"
    assert "После записи" not in plan.content


def test_t07_russian_exact_two_lines_readback_requires_contract() -> None:
    from antigona.task_goal import parse_goal, requires_exact_write_read_contract

    goal = (
        "Создай файл с именем manual test.txt в workspace.\n"
        "Содержимое должно быть ровно двумя строками:\n"
        "ANTIGONA FILE TEST\n"
        "12345\n"
        "После записи прочитай этот же файл обратно и ответь полным отчётом: "
        "путь, tool write, tool read, точное содержимое."
    )

    assert requires_exact_write_read_contract(goal, content=parse_goal(goal).content) is True


def test_t07_exact_content_submission_creates_write_and_readback_steps(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 't07.db'}")
    database.create_all()

    result = TaskSubmissionService(database).submit(
        owner_id="alice",
        message=T07_GOAL,
        idempotency_key="t07",
        tool_name="workspace.write_text",
        path="manual test.txt",
        content=T07_CONTENT,
        read_after_write=False,
        client="unit",
    )

    with database.session_factory() as session:
        task = TaskRepository(session).get(str(result["flow_id"]), "alice")
        steps = sorted(task.steps, key=lambda step: step.index)

    assert [step.tool_name for step in steps] == [
        "workspace.write_text",
        "workspace.read_text",
    ]
    assert steps[0].title == "Execute workspace.write_text"
    assert steps[1].arguments == {"path": "manual test.txt"}


def _client(tmp_path: Path) -> tuple[TestClient, Database]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        f"sqlite:///{tmp_path / 'gateway.db'}",
        workspace,
        {"alice-token": "alice"},
        "inprocess",
        test_mode=True,
    )
    app = create_gateway_app(settings)
    client = TestClient(app)
    client.__enter__()
    return client, app.state.database


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer alice-token"}


def _create_t07_flow(client: TestClient) -> str:
    response = client.post(
        "/flows",
        headers={**_auth(), "Idempotency-Key": "t07", "X-Correlation-Id": "cid"},
        json={
            "goal": T07_GOAL,
            "path": "manual test.txt",
            "content": T07_CONTENT,
            "tool_name": "workspace.write_text",
            "read_after_write": True,
        },
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _mark_done_with_evidence(database: Database, flow_id: str, *, include_read_step: bool) -> None:
    with database.session_factory() as session:
        task = session.get(TaskFlow, flow_id)
        assert task is not None
        task.status = TaskState.DONE.value
        task.updated_at = utcnow()
        write_step = sorted(task.steps, key=lambda step: step.index)[0]
        write_step.output = {
            "ok": True,
            "tool_result": {
                "ok": True,
                "status": "completed",
                "path": "manual test.txt",
                "tool_name": "workspace.write_text",
            },
        }
        session.add(
            Artifact(
                task_id=task.id,
                step_id=write_step.id,
                path="manual test.txt",
                sha256="a" * 64,
                size=len(T07_CONTENT.encode()),
                verified=True,
                evidence=write_step.output,
            )
        )
        if include_read_step:
            read_step = FlowStep(
                task_id=task.id,
                index=1,
                title="Execute workspace.read_text",
                tool_name="workspace.read_text",
                arguments={"path": "manual test.txt"},
                input={"tool_name": "workspace.read_text", "path": "manual test.txt"},
                status="COMPLETED",
                output={
                    "ok": True,
                    "tool_result": {
                        "ok": True,
                        "status": "completed",
                        "stdout_preview": T07_CONTENT,
                        "path": "manual test.txt",
                        "tool_name": "workspace.read_text",
                        "creator_tool": "workspace.write_text",
                    },
                },
            )
            session.add(read_step)
            session.flush()
            session.add(
                Artifact(
                    task_id=task.id,
                    step_id=read_step.id,
                    path="manual test.txt",
                    sha256="a" * 64,
                    size=len(T07_CONTENT.encode()),
                    verified=True,
                    evidence=read_step.output,
                )
            )
        session.add(
            StateTransition(
                task_id=task.id,
                entity_id=task.id,
                entity_type="task",
                from_state="VERIFYING",
                to_state="DONE",
                reason="Verifier passed",
                actor="verifier-service",
                correlation_id="cid",
            )
        )
        session.commit()


def test_t07_final_result_preserves_newline_and_reports_write_read_evidence(tmp_path: Path) -> None:
    client, database = _client(tmp_path)
    flow_id = _create_t07_flow(client)
    _mark_done_with_evidence(database, flow_id, include_read_step=True)

    response = client.get(f"/flows/{flow_id}/result", headers=_auth())
    assert response.status_code == 200
    body = response.json()

    assert body["success"] is True
    safe = body["safe_result_text"]
    assert "Path: manual test.txt" in safe
    assert "Write tool: workspace.write_text" in safe
    assert "Read tool: workspace.read_text" in safe
    assert "Write→read evidence" in safe
    assert "Content:\nANTIGONA FILE TEST\n12345" in safe
    assert "ANTIGONA FILE TEST12345" not in safe


def test_t07_exact_content_result_refuses_success_without_readback_evidence(tmp_path: Path) -> None:
    client, database = _client(tmp_path)
    flow_id = _create_t07_flow(client)
    _mark_done_with_evidence(database, flow_id, include_read_step=False)

    response = client.get(f"/flows/{flow_id}/result", headers=_auth())
    assert response.status_code == 200
    body = response.json()

    assert body["success"] is False
    assert body["safe_result_text"] is None
    assert body["failure_reason"] == "read-back evidence unavailable"
