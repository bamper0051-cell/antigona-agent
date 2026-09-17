"""L7-1 regression: compound diagnostic loop «create→run→diagnose→fix→rerun».

Live defect (owner→bot exam): «Создай buggy_X.py [buggy code]. Запусти его,
пойми почему результат неверный (ожидается 15), исправь код, перезапусти
и покажи исправленный вывод.»

Rule: such goals must parse as ``file_write_fix_run`` (named path + run argv +
4 planned steps: write, run, fix-write, run), route to compound write decision
(never task.shell), keep the named path (not task_output.txt), overwrite the
file cleanly with fixed code only during step 2, and require the final rerun
stdout as the verifying artifact (rejecting write-only artifacts).
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.database import Database
from antigona.repository import CreateTask, TaskRepository
from antigona.router.intent_router import IntentRouter
from antigona.task_goal import parse_goal

DIAGNOSTIC_GOAL = (
    "Создай buggy_EXAM7_89ab.py: def compute_total():\n"
    "    return 10\n"
    "print(compute_total())\n"
    "Запусти его, пойми почему результат неверный (ожидается 15), исправь код, "
    "перезапусти и покажи исправленный вывод."
)

BRACKET_GOAL = (
    "Создай X.py [def compute_total():\n    return 10\nprint(compute_total())]. "
    "Запусти, пойми почему неверно, исправь, перезапусти"
)

BUGGY_SOURCE = "def compute_total():\n    return 10\nprint(compute_total())\n"
FIXED_SOURCE = "def compute_total():\n    return 15\nprint(compute_total())\n"


def test_parse_goal_detects_diagnostic_fix_run_compound() -> None:
    plan = parse_goal(BRACKET_GOAL)

    assert plan.intent == "file_write_fix_run"
    assert plan.path == "X.py"
    assert plan.path != "task_output.txt"
    assert plan.run_after_write is True
    assert plan.fix_after_run is True
    assert "python" in plan.command
    assert "X.py" in plan.command
    assert len(plan.steps) == 4
    assert plan.steps == (
        "workspace.write_text",
        "sandbox.shell",
        "workspace.write_text",
        "sandbox.shell",
    )
    assert "def compute_total" in plan.content


def test_parse_goal_preserves_named_path() -> None:
    plan = parse_goal(DIAGNOSTIC_GOAL)

    assert plan.intent == "file_write_fix_run"
    assert plan.path == "buggy_EXAM7_89ab.py"
    assert plan.path != "task_output.txt"
    assert len(plan.steps) == 4
    assert plan.steps == (
        "workspace.write_text",
        "sandbox.shell",
        "workspace.write_text",
        "sandbox.shell",
    )


def test_router_routes_fix_run_to_compound_not_shell() -> None:
    decision = IntentRouter().route(DIAGNOSTIC_GOAL)

    assert decision.intent == "task.file_write"
    assert decision.intent != "task.shell"
    assert decision.reason_code == "write_fix_run_goal_parser"
    assert decision.entities["path"] == "buggy_EXAM7_89ab.py"
    assert decision.entities["path"] != "task_output.txt"
    assert decision.entities.get("run_after_write") is True
    assert decision.entities.get("fix_after_run") is True
    assert "buggy_EXAM7_89ab.py" in decision.entities["command"]


def test_repository_creates_four_steps_for_fix_run(tmp_path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    with db.session_factory() as session:
        task, created = TaskRepository(session).create(
            CreateTask(
                owner_id="owner",
                goal=DIAGNOSTIC_GOAL,
                path="buggy_EXAM7_89ab.py",
                content=BUGGY_SOURCE,
                idempotency_key="k-l7",
                run_after_write=True,
                run_command=("python", "buggy_EXAM7_89ab.py"),
                fix_after_run=True,
                fix_content=FIXED_SOURCE,
                fix_command=("python", "buggy_EXAM7_89ab.py"),
            )
        )

    assert created is True
    assert task.target_path == "buggy_EXAM7_89ab.py"
    steps = sorted(task.steps, key=lambda s: s.index)
    assert [s.tool_name for s in steps] == [
        "workspace.write_text",
        "sandbox.shell",
        "workspace.write_text",
        "sandbox.shell",
    ]
    # Step 0: initial buggy write
    assert steps[0].arguments.get("content") == BUGGY_SOURCE
    assert steps[0].input == {
        "tool_name": "workspace.write_text",
        "arguments_sha256": task.tool_arguments["arguments_sha256"],
    }
    assert "content" not in steps[0].input
    # Step 1: observe run
    assert steps[1].arguments["command"] == ["python", "buggy_EXAM7_89ab.py"]
    # Step 2: fix write
    import hashlib

    assert steps[2].arguments.get("content") == FIXED_SOURCE
    assert steps[2].input == {
        "tool_name": "workspace.write_text",
        "arguments_sha256": hashlib.sha256(FIXED_SOURCE.encode()).hexdigest(),
    }
    assert "content" not in steps[2].input
    # Step 3: rerun
    assert steps[3].arguments["command"] == ["python", "buggy_EXAM7_89ab.py"]


class _StubShellTool:
    name = "sandbox.shell"

    def __init__(self, workspace) -> None:
        self.workspace = workspace
        self.calls: list[tuple[str, ...]] = []

    def execute(self, arguments):
        from antigona.contracts import ToolResult

        cmd = tuple(arguments.command)
        self.calls.append(cmd)
        target_file = self.workspace / "buggy_EXAM7_89ab.py"
        if target_file.exists():
            content = target_file.read_text()
            if "15" in content:
                return ToolResult(True, "completed", data={"output": "15\n", "exit_code": 0})
            return ToolResult(True, "completed", data={"output": "10\n", "exit_code": 0})
        return ToolResult(True, "completed", data={"output": "10\n", "exit_code": 0})


def test_orchestrator_executes_fix_write_clean_overwrite(tmp_path) -> None:
    """Step 0 writes buggy code, Step 1 runs it, Step 2 overwrites with FIXED code

    only (no appended text, no duplicate def), and Step 3 reruns producing the
    corrected output 15.
    """
    from antigona.config import Settings
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.orchestrator import Orchestrator
    from antigona.verifier_client import VerifierClient

    class _StubVerifier(VerifierClient):
        def __init__(self) -> None:
            pass

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return "REPLAN"

    workspace = tmp_path / "ws"
    settings = Settings(f"sqlite:///{tmp_path / 'x.db'}", workspace, test_mode=True)
    database = Database(f"sqlite:///{tmp_path / 'orch.db'}")
    database.create_all()

    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                owner_id="owner",
                goal=DIAGNOSTIC_GOAL,
                path="buggy_EXAM7_89ab.py",
                content=BUGGY_SOURCE,
                idempotency_key="k-l7-orch",
                run_after_write=True,
                run_command=("python", "buggy_EXAM7_89ab.py"),
                fix_after_run=True,
                fix_content=FIXED_SOURCE,
                fix_command=("python", "buggy_EXAM7_89ab.py"),
            )
        )
        approval = repository.request_approval(repository.get(task.id))
        if approval.decision == "PENDING":
            repository.decide_approval(repository.get(task.id), approval.id, "owner", True)
        tool = WorkspaceFileTool(
            InProcessTestBackend(workspace, test_mode=True), settings.tool_timeout_seconds
        )
        shell = _StubShellTool(workspace)
        Orchestrator(
            session, tool, _StubVerifier(), shell_tool=shell, lease_seconds=1
        ).run(repository.get(task.id), "worker-l7")

        refreshed = repository.get(task.id)

    # 1. The target file contains EXACTLY the fixed code — no appended text block, no double def.
    file_on_disk = (workspace / "buggy_EXAM7_89ab.py").read_text()
    assert file_on_disk == FIXED_SOURCE
    assert file_on_disk.count("def compute_total") == 1
    assert "Исправленный код" not in file_on_disk
    assert "10" not in file_on_disk

    # 2. Shell was called twice: step 1 (observe) and step 3 (rerun)
    assert shell.calls == [
        ("python", "buggy_EXAM7_89ab.py"),
        ("python", "buggy_EXAM7_89ab.py"),
    ]

    # 3. Final artifact is the rerun stdout from step 3 carrying 15 and exit code 0
    last_artifact = sorted(refreshed.artifacts, key=lambda a: a.created_at)[-1]
    assert last_artifact.path.startswith(".antigona-results/")
    artifact_text = (workspace / last_artifact.path).read_text()
    assert "15" in artifact_text
    assert "exit code" in artifact_text.lower()
    assert "0" in artifact_text.split("exit code")[-1]


def _verify_fix_run(
    tmp_path,
    monkeypatch,
    source: str,
    *,
    artifact_path: str | None = None,
    stdout: bytes = b"15\n",
) -> dict[str, Any]:
    import hashlib

    from antigona.models import Artifact, TaskState
    from antigona.verifier import VerifierCriteriaDatabase, VerifierCriteriaStore
    from antigona.verifier.judge import ProviderResult
    from tests.unit.test_verifier_v2 import FakeProvider, run_verify

    url = f"sqlite:///{tmp_path / 'verify.sqlite'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                "owner",
                DIAGNOSTIC_GOAL,
                "buggy_EXAM7_89ab.py",
                BUGGY_SOURCE,
                "idem-l7-verify",
                tool_name="workspace.write_text",
                run_after_write=True,
                run_command=("python", "buggy_EXAM7_89ab.py"),
                fix_after_run=True,
                fix_content=source,
                fix_command=("python", "buggy_EXAM7_89ab.py"),
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "buggy_EXAM7_89ab.py").write_bytes(source.encode("utf-8"))
        if artifact_path is None:
            res_path = f".antigona-results/{task.id}.txt"
            (workspace / ".antigona-results").mkdir(parents=True, exist_ok=True)
            (workspace / res_path).write_bytes(stdout)
            art_data = stdout
            art_save_path = res_path
        else:
            art_save_path = artifact_path
            art_data = source.encode()

        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path=art_save_path,
                sha256=hashlib.sha256(art_data).hexdigest(),
                size=len(art_data),
                evidence={"sha256": hashlib.sha256(art_data).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id

    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "script exists and prints 15")
        criteria_session.commit()
    provider = FakeProvider(
        result=ProviderResult(approved=True, reason="ok", actual_model="verifier")
    )
    return run_verify(url, task_id, workspace, provider, monkeypatch)


def test_verifier_accepts_clean_fixed_script_with_real_stdout(tmp_path, monkeypatch) -> None:
    payload = _verify_fix_run(
        tmp_path,
        monkeypatch,
        FIXED_SOURCE,
        stdout=b"stdout:\n15\n\nexit code:\n0\n",
    )

    assert payload["decision"] == "DONE", payload


def test_verifier_rejects_write_only_artifact_for_fix_run(tmp_path, monkeypatch) -> None:
    """The verifier for fix-run compound rejects a write-only artifact (buggy_EXAM7_89ab.py)

    and requires the final run stdout artifact.
    """
    payload = _verify_fix_run(
        tmp_path,
        monkeypatch,
        FIXED_SOURCE,
        artifact_path="buggy_EXAM7_89ab.py",
    )

    assert payload["decision"] != "DONE", payload
    assert "requires rerun stdout artifact" in payload.get("reason", "").lower() or payload["decision"] == "REPLAN"


@pytest.mark.asyncio
async def test_brain_generates_llm_fix_content_differing_from_buggy_source() -> None:
    """When plan.fix_content is absent/empty, brain drafts the fix via LLM so step 2
    writes corrected code differing from step 0 buggy code.
    """
    from antigona.core.brain import AntigonaBrain, ResponseType

    class _RecordingBackend:
        def __init__(self) -> None:
            self.submits: list[dict[str, Any]] = []

        async def submit_task(self, **kwargs: Any) -> dict[str, Any]:
            self.submits.append(dict(kwargs))
            return {"flow_id": "flow-l7", "id": "flow-l7", "status": "QUEUED"}

        async def cancel_flow(self, flow_id: str) -> None:
            return None

        async def get_flow(self, flow_id: str) -> Any:
            return type("FV", (), {"status": "QUEUED", "artifacts": []})()

        async def steer_flow(self, flow_id: str, message: str) -> None:
            return None

    class _FixDraftEngine:
        async def draft_file_content_result(self, text: str, session_id: str) -> Any:
            if "исправ" in text.lower() or "ошибк" in text.lower() or "15" in text:
                return type(
                    "Draft",
                    (),
                    {
                        "content": FIXED_SOURCE,
                        "status": "DRAFT_OK",
                    },
                )()
            return type(
                "Draft",
                (),
                {
                    "content": BUGGY_SOURCE,
                    "status": "DRAFT_OK",
                },
            )()

        async def close(self) -> None:
            return None

    backend = _RecordingBackend()
    brain = AntigonaBrain(dialogue_engine=_FixDraftEngine(), task_backend=backend)

    response = await brain.process(
        DIAGNOSTIC_GOAL,
        user_id="owner",
        channel="cli",
        session_id="l7-session",
    )

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    submit = backend.submits[0]
    assert submit["path"] == "buggy_EXAM7_89ab.py"
    assert submit["fix_after_run"] is True
    assert submit["run_after_write"] is True
    # Initial content is buggy source (step 0)
    assert "return 10" in submit["content"]
    # Fix content is LLM-generated corrected source (step 2) and NOT identical to buggy initial content
    assert submit["fix_content"] != submit["content"]
    assert "return 15" in submit["fix_content"]
    assert submit["fix_content"] == FIXED_SOURCE


def test_verifier_accepts_fix_run_differing_from_buggy_content_structurally(
    tmp_path, monkeypatch
) -> None:
    import hashlib

    from antigona.models import Artifact, TaskState
    from tests.unit.test_verifier_v2 import FakeProvider, run_verify

    url = f"sqlite:///{tmp_path / 'verify_4steps.sqlite'}"
    workspace = tmp_path / "workspace_4steps"
    workspace.mkdir(parents=True, exist_ok=True)
    db = Database(url)
    db.create_all()

    stdout_step1 = b"stdout:\ntotal = 10\n\nexit code:\n0\n"
    stdout_step3 = b"stdout:\ntotal = 15\n\nexit code:\n0\n"

    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                "owner",
                DIAGNOSTIC_GOAL,
                "buggy_EXAM7_89ab.py",
                BUGGY_SOURCE,
                "idem-l7-verify-4steps",
                tool_name="workspace.write_text",
                run_after_write=True,
                run_command=("python", "buggy_EXAM7_89ab.py"),
                fix_after_run=True,
                fix_content=FIXED_SOURCE,
                fix_command=("python", "buggy_EXAM7_89ab.py"),
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")

        # Step 0: buggy script written
        art0_data = BUGGY_SOURCE.encode("utf-8")
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="buggy_EXAM7_89ab.py",
                sha256=hashlib.sha256(art0_data).hexdigest(),
                size=len(art0_data),
                evidence={"sha256": hashlib.sha256(art0_data).hexdigest()},
            )
        )

        # Step 1: initial run stdout
        res_path = f".antigona-results/{task.id}.txt"
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[1].id,
                path=res_path,
                sha256=hashlib.sha256(stdout_step1).hexdigest(),
                size=len(stdout_step1),
                evidence={"sha256": hashlib.sha256(stdout_step1).hexdigest()},
            )
        )

        # Step 2: fixed script written (differs in content and size from task.content)
        art2_data = FIXED_SOURCE.encode("utf-8")
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[2].id,
                path="buggy_EXAM7_89ab.py",
                sha256=hashlib.sha256(art2_data).hexdigest(),
                size=len(art2_data),
                evidence={"sha256": hashlib.sha256(art2_data).hexdigest()},
            )
        )

        # Step 3: rerun stdout
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[3].id,
                path=res_path,
                sha256=hashlib.sha256(stdout_step3).hexdigest(),
                size=len(stdout_step3),
                evidence={"sha256": hashlib.sha256(stdout_step3).hexdigest()},
            )
        )

        # Write actual workspace files corresponding to the final state:
        (workspace / "buggy_EXAM7_89ab.py").write_text(FIXED_SOURCE)
        (workspace / ".antigona-results").mkdir(parents=True, exist_ok=True)
        (workspace / res_path).write_bytes(stdout_step3)

        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id

    # Notice: NO criteria put into VerifierCriteriaStore, proving structural verification
    provider = FakeProvider()
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)

    assert payload["decision"] == "DONE", payload

    with db.session_factory() as session:
        task_after = TaskRepository(session).get(task_id)
        assert task_after.status == TaskState.DONE.value
        # The final step3 artifact must be verified=True
        step3_art = next(
            a for a in task_after.artifacts
            if a.step_id == task_after.steps[3].id
        )
        assert step3_art.verified is True
        assert step3_art.evidence.get("judge_model") == "structural"


def test_cli_generates_llm_fix_content_differing_from_buggy_source(monkeypatch) -> None:
    """CLI-path file_write_fix_run with empty plan.fix_content produces a non-empty
    LLM-generated fix_content (distinct from buggy), so the fix write step receives
    corrected code and create_flow succeeds with 4 planned steps."""
    from typer.testing import CliRunner

    import antigona.cli as cli
    from antigona.conversation.dialogue_engine import DialogueEngine, FileContentDraft

    created_flows: list[dict[str, Any]] = []

    async def _mock_create_flow(self, **kwargs: Any) -> dict[str, Any]:
        created_flows.append(dict(kwargs))
        return {
            "id": "flow-cli-l7",
            "correlation_id": kwargs.get("correlation_id", "cid-cli-l7"),
            "status": "RECEIVED",
        }

    async def _mock_draft_result(self, text: str, session_id: str = "cli-run") -> FileContentDraft:
        if "исправ" in text.lower() or "ошибк" in text.lower() or "15" in text:
            return FileContentDraft(FIXED_SOURCE, "DRAFT_OK")
        return FileContentDraft(BUGGY_SOURCE, "DRAFT_OK")

    monkeypatch.setattr(cli.GatewayClient, "create_flow", _mock_create_flow)
    monkeypatch.setattr(DialogueEngine, "draft_file_content_result", _mock_draft_result)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["run", DIAGNOSTIC_GOAL, "--no-attach", "--token", "tok-test"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert len(created_flows) == 1
    flow_req = created_flows[0]
    assert flow_req["path"] == "buggy_EXAM7_89ab.py"
    assert flow_req["fix_after_run"] is True
    assert flow_req["run_after_write"] is True
    # Initial content is buggy source (step 0)
    assert "return 10" in flow_req["content"]
    # Fix content is LLM-generated corrected source (step 2) and NOT identical to buggy initial content
    assert flow_req["fix_content"] != flow_req["content"]
    assert "return 15" in flow_req["fix_content"]
    assert flow_req["fix_content"] == FIXED_SOURCE


def test_cli_fails_cleanly_when_llm_fix_draft_fails(monkeypatch) -> None:
    """If LLM draft is unavailable / empty, CLI exits cleanly with error and does not create flow."""
    from typer.testing import CliRunner

    import antigona.cli as cli
    from antigona.conversation.dialogue_engine import DialogueEngine, FileContentDraft

    created_flows: list[dict[str, Any]] = []

    async def _mock_create_flow(self, **kwargs: Any) -> dict[str, Any]:
        created_flows.append(dict(kwargs))
        return {"id": "flow-1", "status": "RECEIVED"}

    async def _mock_draft_result(self, text: str, session_id: str = "cli-run") -> FileContentDraft:
        return FileContentDraft(None, "DRAFT_UNAVAILABLE")

    monkeypatch.setattr(cli.GatewayClient, "create_flow", _mock_create_flow)
    monkeypatch.setattr(DialogueEngine, "draft_file_content_result", _mock_draft_result)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["run", DIAGNOSTIC_GOAL, "--no-attach", "--token", "tok-test"],
    )

    assert result.exit_code != 0
    assert len(created_flows) == 0


def test_repository_rejects_empty_fix_content_for_fix_run(tmp_path) -> None:
    """TaskRepository refuses to create a fix_run task if fix_content is empty."""
    from antigona.repository import SensitiveTaskInput

    db = Database(f"sqlite:///{tmp_path / 'db_empty_fix.sqlite'}")
    db.create_all()
    with db.session_factory() as session:
        with pytest.raises(SensitiveTaskInput, match="fix_after_run requires non-empty fix_content"):
            TaskRepository(session).create(
                CreateTask(
                    owner_id="owner",
                    goal=DIAGNOSTIC_GOAL,
                    path="buggy_EXAM7_89ab.py",
                    content=BUGGY_SOURCE,
                    idempotency_key="k-l7-empty",
                    run_after_write=True,
                    run_command=("python", "buggy_EXAM7_89ab.py"),
                    fix_after_run=True,
                    fix_content="",
                    fix_command=("python", "buggy_EXAM7_89ab.py"),
                )
            )



