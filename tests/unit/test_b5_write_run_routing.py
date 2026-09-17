"""B5 regression: compound "create a .py file from a description, then run it".

Live defect (owner→bot exam, SHA 1fd44ddb): «Создай square_X.py: печатает квадрат
argv[1]. Запусти python square_X.py 12» parsed as intent='shell' with EMPTY
path/content, so the planner built `sh -c "<whole russian goal>"`; the agent lost
the named filename, wrote the source into the default task_output.txt (with
markdown fences) and never ran the program.

Rule: such goals must parse as ``file_write_run`` (named path + run argv +
description hint), route to the compound write decision (never task.shell), keep
the named path, and create a sandbox.shell run step after the write step.
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.core.brain import AntigonaBrain, ResponseType
from antigona.database import Database
from antigona.repository import CreateTask, TaskRepository
from antigona.router.intent_router import IntentRouter
from antigona.task_goal import parse_goal, strip_code_fences

WRITE_RUN = (
    "Создай square_EXAM23_43df.py: печатает квадрат argv[1]. "
    "Запусти python square_EXAM23_43df.py 12. Пришли stdout и exit code."
)


class _RecordingBackend:
    def __init__(self) -> None:
        self.submits: list[dict[str, Any]] = []

    async def submit_task(self, **kwargs: Any) -> dict[str, Any]:
        self.submits.append(dict(kwargs))
        return {"flow_id": "flow-b5", "id": "flow-b5", "status": "QUEUED"}

    async def cancel_flow(self, flow_id: str) -> None:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED", "artifacts": []})()

    async def steer_flow(self, flow_id: str, message: str) -> None:
        return None


class _FencedDraft:
    """LLM draft that wraps the source in markdown fences (canon forbids them)."""

    async def draft_file_content_result(self, text: str, session_id: str) -> Any:
        return type(
            "Draft",
            (),
            {
                "content": "```python\nimport sys\nprint(int(sys.argv[1]) ** 2)\n```",
                "status": "DRAFT_OK",
            },
        )()

    async def close(self) -> None:
        return None


def test_parse_goal_detects_write_run_compound() -> None:
    plan = parse_goal(WRITE_RUN)

    assert plan.intent == "file_write_run"
    assert plan.path == "square_EXAM23_43df.py"
    assert plan.run_after_write is True
    assert "square_EXAM23_43df.py" in plan.command
    assert "12" in plan.command
    assert plan.content_hint == "печатает квадрат argv[1]"
    # The description is a hint for code generation, never the file body.
    assert plan.content == ""


def test_router_routes_write_run_to_compound_not_shell() -> None:
    decision = IntentRouter().route(WRITE_RUN)

    assert decision.intent == "task.file_write"
    assert decision.intent != "task.shell"
    assert decision.reason_code == "write_then_run_goal_parser"
    assert decision.entities["path"] == "square_EXAM23_43df.py"
    assert decision.entities.get("run_after_write") is True
    assert "square_EXAM23_43df.py" in decision.entities["command"]
    assert "12" in decision.entities["command"]


def test_router_preserves_named_path_not_task_output() -> None:
    decision = IntentRouter().route(WRITE_RUN)

    assert decision.entities["path"] != "task_output.txt"


def test_strip_code_fences_removes_markdown_wrapper() -> None:
    fenced = "```python\nimport sys\nprint(1)\n```"

    assert strip_code_fences(fenced) == "import sys\nprint(1)\n"
    assert strip_code_fences("import sys\n") == "import sys\n"


# Точная форма грязного черновика из live-прогона на SHA 9bbcc017: валидный код,
# затем встроенные фенсы и хвостовая секция stdout/exit code.
POLLUTED_DRAFT = """import sys

if len(sys.argv) > 1:
    print(float(sys.argv[1]) ** 2)
else:
    print("usage: square.py N")
    sys.exit(1)
```
(stdout: 144.0 / exit code: 0)   <-- markdown fences + trailing noise
```
"""

CLEAN_SOURCE = """import sys

if len(sys.argv) > 1:
    print(float(sys.argv[1]) ** 2)
else:
    print("usage: square.py N")
    sys.exit(1)
"""


def test_strip_code_fences_cleans_real_polluted_draft() -> None:
    import ast

    cleaned = strip_code_fences(POLLUTED_DRAFT)

    assert cleaned == CLEAN_SOURCE
    assert "```" not in cleaned
    assert "stdout" not in cleaned
    assert "exit code" not in cleaned
    ast.parse(cleaned)  # записанный файл обязан быть исполняемым python

    # Идемпотентность: повторная чистка ничего не меняет.
    assert strip_code_fences(cleaned) == cleaned


def test_strip_code_fences_removes_embedded_fences_without_report() -> None:
    draft = "```python\nimport sys\n```\n```\nprint(1)\n```"

    assert strip_code_fences(draft) == "import sys\nprint(1)\n"


def test_strip_code_fences_truncates_trailing_report_sections() -> None:
    for marker in ("stdout:", "stdout=", "exit code:", "Exit code=", "Вывод:", "Output:"):
        draft = f"print(1)\n{marker} 144.0\n"

        assert strip_code_fences(draft) == "print(1)\n"


def test_strip_code_fences_does_not_over_trim_legit_source() -> None:
    literal = 'print("stdout: ready")\nprint("exit code: 0")\n'
    annotation = "output: int = 5\nprint(output)\n"

    assert strip_code_fences(literal) == literal
    assert strip_code_fences(annotation) == annotation


# Live-черновик round 3 (SHA eece01ee): валидный код + хвостовая РУССКАЯ
# аннотация, которая не является отчётным маркером, но ломает compile().
ANNOTATED_DRAFT = """import sys
if len(sys.argv) != 2:
    print("Usage: ...")
    sys.exit(1)
try:
    number = float(sys.argv[1])
    print(number ** 2)
except ValueError:
    print("Error: Argument must be a number.")
    sys.exit(1)

После выполнения команды `python square_EXAM23_56b8.py 12`:
"""

ANNOTATED_CLEAN = """import sys
if len(sys.argv) != 2:
    print("Usage: ...")
    sys.exit(1)
try:
    number = float(sys.argv[1])
    print(number ** 2)
except ValueError:
    print("Error: Argument must be a number.")
    sys.exit(1)
"""


def test_strip_code_fences_truncates_trailing_annotation() -> None:
    cleaned = strip_code_fences(ANNOTATED_DRAFT, path="square_EXAM23_56b8.py")

    assert cleaned == ANNOTATED_CLEAN
    assert "После выполнения" not in cleaned
    compile(cleaned, "square_EXAM23_56b8.py", "exec")

    # Идемпотентность и отсутствие over-trim для уже чистого кода.
    assert strip_code_fences(cleaned, path="square_EXAM23_56b8.py") == cleaned
    assert (
        strip_code_fences(
            "import sys\nprint(int(sys.argv[1]) ** 2)\n", path="square.py"
        )
        == "import sys\nprint(int(sys.argv[1]) ** 2)\n"
    )
    # Обычный текстовый файл не трогаем — compile-логика только для .py.
    assert strip_code_fences(ANNOTATED_DRAFT, path="notes.txt") == ANNOTATED_DRAFT


def test_repository_creates_run_step_after_write_step(tmp_path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    with db.session_factory() as session:
        task, created = TaskRepository(session).create(
            CreateTask(
                owner_id="owner",
                goal=WRITE_RUN,
                path="square_EXAM23_43df.py",
                content="import sys\nprint(int(sys.argv[1]) ** 2)\n",
                idempotency_key="k-b5",
                run_after_write=True,
                run_command=("python", "square_EXAM23_43df.py", "12"),
            )
        )

    assert created is True
    assert task.target_path == "square_EXAM23_43df.py"
    steps = sorted(task.steps, key=lambda s: s.index)
    assert [s.tool_name for s in steps] == ["workspace.write_text", "sandbox.shell"]
    assert steps[1].arguments["command"] == [
        "python",
        "square_EXAM23_43df.py",
        "12",
    ]


class _StubShellTool:
    """Shell stand-in: records the argv and returns the program's stdout."""

    name = "sandbox.shell"

    def __init__(self, workspace) -> None:
        self.workspace = workspace
        self.calls: list[tuple[str, ...]] = []

    def execute(self, arguments):
        from antigona.contracts import ToolResult

        self.calls.append(tuple(arguments.command))
        return ToolResult(True, "completed", data={"output": "144\n", "exit_code": 0})


def test_orchestrator_writes_named_file_then_runs_it(tmp_path) -> None:
    """Step 0 writes the named script; step 1 runs it and its STDOUT becomes
    the final artifact (not a re-read of the script file)."""
    from antigona.config import Settings
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.orchestrator import Orchestrator
    from antigona.verifier_client import VerifierClient

    class _StubVerifier(VerifierClient):
        def __init__(self) -> None:
            self.decision = "REPLAN"

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return self.decision

    workspace = tmp_path / "ws"
    settings = Settings(f"sqlite:///{tmp_path / 'x.db'}", workspace, test_mode=True)
    database = Database(f"sqlite:///{tmp_path / 'orch.db'}")
    database.create_all()
    source = "import sys\nprint(int(sys.argv[1]) ** 2)\n"
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                owner_id="owner",
                goal=WRITE_RUN,
                path="square_EXAM23_43df.py",
                content=source,
                idempotency_key="k-b5-orch",
                run_after_write=True,
                run_command=("python", "square_EXAM23_43df.py", "12"),
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
        ).run(repository.get(task.id), "worker-b5")

        refreshed = repository.get(task.id)

    # 1. The named script was written — never the default task_output.txt.
    assert (workspace / "square_EXAM23_43df.py").read_text() == source
    assert not (workspace / "task_output.txt").exists()
    # 2. The run step really executed the requested command.
    assert shell.calls == [("python", "square_EXAM23_43df.py", "12")]
    # 3. The final artifact carries the program's STDOUT (144), not the source.
    last_artifact = sorted(refreshed.artifacts, key=lambda a: a.created_at)[-1]
    assert last_artifact.path.startswith(".antigona-results/")
    artifact_text = (workspace / last_artifact.path).read_text()
    assert "144" in artifact_text
    # 4. The goal asks for stdout AND the exit code — both live in the artifact.
    assert "exit code" in artifact_text.lower()
    assert "0" in artifact_text.split("exit code")[-1]
    # The source file itself stays clean — the run result is not appended to it.
    assert (workspace / "square_EXAM23_43df.py").read_text() == source


def _verify_write_run(
    tmp_path,
    monkeypatch,
    source: str,
    stdout: bytes = b"144\n",
    context: dict[str, str] | None = None,
) -> dict[str, str]:
    """Drive the real verifier over a finished write→run task."""
    import hashlib

    from antigona.models import Artifact, TaskState
    from antigona.verifier import VerifierCriteriaDatabase, VerifierCriteriaStore
    from antigona.verifier.judge import ProviderResult
    from tests.unit.test_verifier_v2 import FakeProvider, run_verify

    url = f"sqlite:///{tmp_path / 'verify.sqlite'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                "owner",
                WRITE_RUN,
                "square_EXAM23_43df.py",
                source,
                "idem-b5-verify",
                tool_name="workspace.write_text",
                run_after_write=True,
                run_command=("python", "square_EXAM23_43df.py", "12"),
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "square_EXAM23_43df.py").write_text(source)
        result_path = f".antigona-results/{task.id}.txt"
        (workspace / ".antigona-results").mkdir(parents=True, exist_ok=True)
        (workspace / result_path).write_bytes(stdout)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path=result_path,
                sha256=hashlib.sha256(stdout).hexdigest(),
                size=len(stdout),
                evidence={"sha256": hashlib.sha256(stdout).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    if context is not None:
        context["url"] = url
        context["task_id"] = task_id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "script exists and prints 144")
        criteria_session.commit()
    provider = FakeProvider(
        result=ProviderResult(approved=True, reason="ok", actual_model="verifier")
    )
    return run_verify(url, task_id, workspace, provider, monkeypatch)


def test_verifier_accepts_clean_script_with_real_stdout(tmp_path, monkeypatch) -> None:
    payload = _verify_write_run(
        tmp_path, monkeypatch, "import sys\nprint(int(sys.argv[1]) ** 2)\n"
    )

    assert payload["decision"] == "DONE", payload


def test_verifier_rejects_fenced_script(tmp_path, monkeypatch) -> None:
    """A markdown-fenced .py file is not executable — DONE is forbidden."""
    payload = _verify_write_run(
        tmp_path,
        monkeypatch,
        "```python\nimport sys\nprint(int(sys.argv[1]) ** 2)\n```\n",
    )

    assert payload["decision"] != "DONE", payload


def test_delivered_result_message_preserves_run_stdout_newlines(
    tmp_path, monkeypatch
) -> None:
    """Canon: "stdout:\\n144.0" must never be delivered as "stdout:144.0"."""
    from sqlalchemy import select

    from antigona.models import DeliveryOutbox

    context: dict[str, str] = {}
    payload = _verify_write_run(
        tmp_path,
        monkeypatch,
        "import sys\nprint(float(sys.argv[1]) ** 2)\n",
        stdout=b"stdout:\n144.0\n\nexit code:\n0\n",
        context=context,
    )
    assert payload["decision"] == "DONE", payload

    db = Database(context["url"])
    with db.session_factory() as session:
        rows = list(
            session.scalars(
                select(DeliveryOutbox).where(
                    DeliveryOutbox.task_id == context["task_id"],
                    DeliveryOutbox.event_type == "result",
                )
            )
        )
    assert rows, "verifier produced no result outbox row"
    message = rows[0].payload["message"]
    assert "stdout:\n144.0" in message, message
    assert "144.0\n\nexit code:\n0" in message.replace("\n\n\n", "\n\n"), message
    assert "stdout:144.0" not in message, message
    assert "144.0exit code" not in message, message


@pytest.mark.asyncio
async def test_brain_submits_named_path_run_command_and_unfenced_source() -> None:
    backend = _RecordingBackend()
    brain = AntigonaBrain(dialogue_engine=_FencedDraft(), task_backend=backend)

    response = await brain.process(
        WRITE_RUN,
        user_id="owner",
        channel="cli",
        session_id="b5-session",
    )

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    submit = backend.submits[0]
    assert submit["tool_name"] in (None, "workspace.write_text")
    assert submit["path"] == "square_EXAM23_43df.py"
    assert submit["path"] != "task_output.txt"
    assert submit["run_after_write"] is True
    assert list(submit["run_command"]) == ["python", "square_EXAM23_43df.py", "12"]
    assert "```" not in submit["content"]
    assert submit["content"].startswith("import sys")


def _write_run_task(tmp_path, *, dbname, key, run_command, goal=WRITE_RUN):
    db = Database(f"sqlite:///{tmp_path / dbname}")
    db.create_all()
    session = db.session_factory()
    repository = TaskRepository(session)
    task, _ = repository.create(
        CreateTask(
            owner_id="owner",
            goal=goal,
            path="square_EXAM23_43df.py",
            content="import sys\nprint(int(sys.argv[1]) ** 2)\n",
            idempotency_key=key,
            run_after_write=True,
            run_command=run_command,
        )
    )
    return repository, repository.get(task.id)


def test_write_run_safe_run_step_auto_approves(tmp_path) -> None:
    """B5 live round 6: the deterministic write→run compound must not hang in
    WAITING_APPROVAL because its system-built `python <script> <arg>` run step
    classifies as MEDIUM."""
    from antigona.worker.hitl import (
        ConfirmationPolicyMode,
        get_confirmation_policy,
        set_confirmation_policy,
    )

    original = get_confirmation_policy()
    set_confirmation_policy(ConfirmationPolicyMode.ALWAYS)
    try:
        repository, task = _write_run_task(
            tmp_path,
            dbname="b5-auto.sqlite",
            key="k-b5-auto",
            run_command=("python", "square_EXAM23_43df.py", "12"),
        )
        approval = repository.request_approval(task)
        assert approval.risk_level == "LOW"
        assert approval.decision == "APPROVED"
    finally:
        set_confirmation_policy(original)


def test_write_run_destructive_run_step_still_requires_approval(tmp_path) -> None:
    """Control: a shell step that is not the provably-safe system-built run of
    the created script keeps the fail-closed approval gate."""
    from antigona.worker.hitl import (
        ConfirmationPolicyMode,
        get_confirmation_policy,
        set_confirmation_policy,
    )

    original = get_confirmation_policy()
    set_confirmation_policy(ConfirmationPolicyMode.ALWAYS)
    try:
        repository, task = _write_run_task(
            tmp_path,
            dbname="b5-destructive.sqlite",
            key="k-b5-destructive",
            run_command=("rm", "-rf", "square_EXAM23_43df.py"),
        )
        approval = repository.request_approval(task)
        assert approval.decision == "PENDING"
        assert approval.risk_level == "HIGH"

        repository2, task2 = _write_run_task(
            tmp_path,
            dbname="b5-piped.sqlite",
            key="k-b5-piped",
            run_command=("python", "square_EXAM23_43df.py", "12", "|", "tee", "out.txt"),
        )
        piped = repository2.request_approval(task2)
        assert piped.decision == "PENDING"
        assert piped.risk_level == "MEDIUM"

        repository3, task3 = _write_run_task(
            tmp_path,
            dbname="b5-other.sqlite",
            key="k-b5-other",
            run_command=("python", "other_script.py"),
        )
        other = repository3.request_approval(task3)
        assert other.decision == "PENDING"
        assert other.risk_level == "MEDIUM"
    finally:
        set_confirmation_policy(original)
