"""Regression tests for Wave 6b: 'ACCEPT -> ERROR' root causes.

Verifies:
1. Multi-file goal with paths already containing folder does NOT duplicate folder prefixes,
   and generated command works in the real sandbox.
2. Literal tool prefixes ('shell:', 'bash:', 'sh:', 'execute:') are stripped at ingestion/entry,
   and prefixed commands run successfully.
3. Non-zero sandbox exit surfaces honest returncode and diagnostic with strict secret redaction.
4. Refused/blocked actions are never reported with '✅ Готово' and have non-success terminal status.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from antigona.durable.operation_models import OperationState
from antigona.events.event_types import FinalResponseReady
from antigona.presentation.presenter import OperationPresenter
from antigona.sandbox.runner import GVISOR_RUNTIME, runtime_registered
from antigona.shell import DockerShellTool, ShellInput
from antigona.task_goal import _build_multi_file_command, parse_goal
from antigona.tools.shell_command import (
    strip_shell_tool_prefix,
    strip_shell_tool_prefix_argv,
)

# ── W6b-1: Multi-file Command Generation without Folder Duplication ─────────────

def _require_gvisor_runtime() -> None:
    """Skip the REAL-container tests where gVisor is not registered.

    ``DockerShellTool(runtime="runsc")`` is fail-closed on purpose: without a
    registered ``runsc`` runtime it refuses to execute anything rather than
    silently falling back to the shared-host-kernel ``runc``.  Hosts without
    gVisor (e.g. a stock CI runner) therefore have nothing to assert here; the
    refusal/fail-closed contract itself is covered by
    ``tests/sandbox/test_isolation_failclosed.py`` and ``tests/sandbox/test_escape.py``.
    """
    if not runtime_registered(GVISOR_RUNTIME):
        pytest.skip(f"{GVISOR_RUNTIME} runtime is not registered with docker")





def test_build_multi_file_command_no_values_with_prefixed_files() -> None:
    """Multi-file goal whose file names already contain folder has NO duplicated prefix."""
    data = {
        "folder": "exam/p1",
        "files": ["exam/p1/a.txt", "exam/p1/b.txt"],
        "pairs": [],
    }
    cmd, path, content = _build_multi_file_command(data)
    assert "exam/p1/exam/p1" not in cmd
    assert cmd == "mkdir -p exam/p1 && touch exam/p1/a.txt && touch exam/p1/b.txt && find exam/p1 -type f | sort"
    assert path == "stdout"


def test_build_multi_file_command_no_values_with_bare_files() -> None:
    """Multi-file goal with bare file names prepends folder once."""
    data = {
        "folder": "exam/p1",
        "files": ["a.txt", "b.txt"],
        "pairs": [],
    }
    cmd, path, content = _build_multi_file_command(data)
    assert "exam/p1/exam/p1" not in cmd
    assert cmd == "mkdir -p exam/p1 && touch exam/p1/a.txt && touch exam/p1/b.txt && find exam/p1 -type f | sort"


def test_build_multi_file_command_pairs_with_prefixed_files() -> None:
    """Multi-file goal with content pairs whose file names contain folder has NO duplicated prefix."""
    data = {
        "folder": "exam/p1",
        "files": ["exam/p1/a.txt", "exam/p1/b.txt"],
        "pairs": [("exam/p1/a.txt", "alpha"), ("exam/p1/b.txt", "beta")],
    }
    cmd, path, content = _build_multi_file_command(data)
    assert "exam/p1/exam/p1" not in cmd
    assert "printf 'alpha\\n' > exam/p1/a.txt" in cmd
    assert "printf 'beta\\n' > exam/p1/b.txt" in cmd
    assert path == "exam/p1/summary.txt"


def test_parse_goal_multi_file_no_duplicated_folder() -> None:
    """parse_goal produces a valid command without duplicate folder path."""
    plan = parse_goal("Создай папку exam/p1. Создай exam/p1/a.txt, exam/p1/b.txt")
    assert plan.intent == "multi_file"
    assert "exam/p1/exam/p1" not in plan.command
    assert "touch exam/p1/a.txt" in plan.command
    assert "touch exam/p1/b.txt" in plan.command


def test_generated_multi_file_command_executes_in_real_sandbox() -> None:
    """The command produced by the fixed generator executes successfully (rc=0) in gVisor sandbox."""
    _require_gvisor_runtime()
    data = {
        "folder": "exam/p1",
        "files": ["exam/p1/a.txt", "exam/p1/b.txt"],
        "pairs": [],
    }
    cmd, _, _ = _build_multi_file_command(data)
    with tempfile.TemporaryDirectory() as td:
        tool = DockerShellTool(workspace=Path(td), runtime="runsc")
        result = tool.execute(ShellInput(command=(cmd,)))
        assert result.ok is True
        assert result.status == "completed"
        out = result.data.get("output", "")
        assert "exam/p1/a.txt" in out
        assert "exam/p1/b.txt" in out


# ── W6b-2: Tool Prefix Stripping ───────────────────────────────────────────────


def test_strip_shell_tool_prefix_strings() -> None:
    """Tool prefixes are cleanly stripped from single command strings."""
    assert strip_shell_tool_prefix("shell: uname -a") == "uname -a"
    assert strip_shell_tool_prefix("bash: ls -la") == "ls -la"
    assert strip_shell_tool_prefix("sh: echo hello") == "echo hello"
    assert strip_shell_tool_prefix("execute: pwd") == "pwd"
    assert strip_shell_tool_prefix("shell:echo C5_APPROVED_SHELL_OK && uname -s") == "echo C5_APPROVED_SHELL_OK && uname -s"
    assert strip_shell_tool_prefix("SHELL: uname -a") == "uname -a"
    assert strip_shell_tool_prefix("echo hello") == "echo hello"


def test_strip_shell_tool_prefix_argv() -> None:
    """Tool prefixes are cleanly stripped from argv tuples and lists."""
    assert strip_shell_tool_prefix_argv(("shell: uname -a",)) == ("uname -a",)
    assert strip_shell_tool_prefix_argv(("shell:", "uname", "-a")) == ("uname", "-a")
    assert strip_shell_tool_prefix_argv(("bash: uname", "-a")) == ("uname", "-a")
    assert strip_shell_tool_prefix_argv(("execute:", "pwd")) == ("pwd",)
    assert strip_shell_tool_prefix_argv(("uname", "-a")) == ("uname", "-a")


def test_shell_prefixed_command_runs_in_sandbox() -> None:
    """A command passed with 'shell:' prefix runs successfully in the sandbox."""
    _require_gvisor_runtime()
    with tempfile.TemporaryDirectory() as td:
        tool = DockerShellTool(workspace=Path(td), runtime="runsc")
        result = tool.execute(ShellInput(command=("shell: uname -s",)))
        assert result.ok is True
        assert result.status == "completed"
        assert "Linux" in str(result.data.get("output", ""))


# ── W6b-3: Honest Non-Zero Sandbox Exit with Strict Redaction ───────────────────


def test_nonzero_exit_surfaces_returncode_and_diagnostic() -> None:
    """Non-zero exit surfaces exit code, diagnostic, and redacted stderr excerpt."""
    _require_gvisor_runtime()
    with tempfile.TemporaryDirectory() as td:
        tool = DockerShellTool(workspace=Path(td), runtime="runsc")
        result = tool.execute(ShellInput(command=("nonexistent_command_xyz",)))
        assert result.ok is False
        assert result.status == "failed"
        assert "tool exited non-zero" in result.error
        assert "exit code 127" in result.error
        assert "command_not_found" in result.error
        # Evidence objects are present
        evidence_dict = {e.kind: e.value for e in result.evidence}
        assert evidence_dict.get("returncode") == "127"
        assert evidence_dict.get("diagnostic") == "command_not_found"


def test_nonzero_exit_strictly_redacts_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Secrets or synthetic passwords in stderr are NEVER exposed in error message."""
    import subprocess as subprocess_module
    marker = b"synthetic-password-leak-12345"

    class FakeProcess:
        returncode = 1

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            return b"", b"Error: secret=" + marker

    monkeypatch.setattr(subprocess_module, "Popen", lambda *a, **k: FakeProcess())
    tool = DockerShellTool(workspace=tmp_path / "ws")
    result = tool.execute(ShellInput(command=("sh", "-c", "some_cmd")))
    assert result.ok is False
    assert marker.decode() not in result.error
    assert marker.decode() not in repr(result)
    assert "secret=" not in result.error


# ── W6b-4: Refusal and Blocked Status Presentation ─────────────────────────────


@pytest.mark.asyncio
async def test_refusal_is_never_reported_as_gotovo() -> None:
    """A refusal message is never rendered with '✅ Готово' header."""
    refusal_text = (
        "🚫 Действие отклонено защитой рабочей области: запрошенная "
        "операция выходит за разрешённые границы. Изменений не внесено."
    )

    chunks = OperationPresenter._render_final_chunks(
        FinalResponseReady(
            operation_id="op-1",
            text=refusal_text,
            terminal_state="SUCCEEDED",
            completed_action=True,  # Mistakenly set upstream
        ),
        status=OperationState.SUCCEEDED,
        plain=True,
    )
    rendered = "\n".join(chunks)
    assert "✅ Готово" not in rendered
    assert "✅ <b>Готово</b>" not in rendered
    assert "❌ Не выполнено" in rendered


@pytest.mark.asyncio
async def test_failed_status_header_is_honest() -> None:
    """OperationState.FAILED renders '❌ Не выполнено', never success."""
    chunks = OperationPresenter._render_final_chunks(
        FinalResponseReady(
            operation_id="op-2",
            text="Задача не выполнена: tool exited non-zero (exit code 1, command_not_found)",
            terminal_state="FAILED",
            completed_action=False,
        ),
        status=OperationState.FAILED,
        plain=True,
    )
    rendered = "\n".join(chunks)
    assert "✅ Готово" not in rendered
    assert "❌ Не выполнено" in rendered
