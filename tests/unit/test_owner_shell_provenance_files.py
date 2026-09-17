"""Owner Mode execution, tool-result provenance, and file-operation regression tests.

Covers three areas investigated together because they share one root
principle: Antigona must only report what a real tool/executor actually did,
never a fabricated result.

1. Owner Mode -> /shell actually reaches host execution with real stdout/exit
   code, and is denied when the PIN gate never unlocked.
2. Bare-command grounding: a direct-shell command's real result (success or
   [TOOL_ERROR]) is persisted into session history so a later conversational
   turn cannot hallucinate what happened.
3. File tools (write_file/read_file) return real, resolved paths and honest
   NOT_FOUND/PERMISSION errors -- never invented content or paths.
"""

from __future__ import annotations

import json
import sys
import uuid

import pytest

from antigona.contracts import ToolResult
from antigona.core import paths
from antigona.core.brain import AntigonaBrain, ResponseType, _is_bare_shell_command
from antigona.engine.unified_executor import (
    OwnerAuthContext,
    ToolExecutionRequest,
    UnifiedToolExecutionLayer,
)
from antigona.tools.owner_shell import OwnerShellDenied, run_owner_shell
from antigona.tools.registry import ToolRegistry, register_builtins

# ── Owner Mode: real execution vs real denial ───────────────────────────────


def test_owner_shell_denied_without_pin() -> None:
    """Regular user / no PIN gate: is_owner=False must hard-deny, never run."""
    with pytest.raises(OwnerShellDenied):
        run_owner_shell("echo should-not-run", is_owner=False)


@pytest.mark.skipif(sys.platform == "win32", reason='subprocess pass_fds is not supported on Windows (Wave 4)')
def test_owner_shell_runs_real_command_after_pin() -> None:
    """is_owner=True (the live PIN-gate result) actually executes on the host."""
    result = run_owner_shell("echo owner-mode-real-output", is_owner=True)
    assert result.exit_code == 0
    assert "owner-mode-real-output" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason='subprocess pass_fds is not supported on Windows (Wave 4)')
@pytest.mark.skipif(sys.platform == "win32", reason='subprocess pass_fds is not supported on Windows (Wave 4)')
def test_owner_shell_nonzero_exit_is_not_success() -> None:
    """A real failing command must surface its real exit code, not SUCCESS."""
    result = run_owner_shell("exit 7", is_owner=True)
    assert result.exit_code == 7


@pytest.mark.skipif(sys.platform == "win32", reason='subprocess pass_fds is not supported on Windows (Wave 4)')
def test_owner_shell_stderr_preserved() -> None:
    result = run_owner_shell("echo real-stderr-text 1>&2", is_owner=True)
    assert "real-stderr-text" in result.stderr


@pytest.mark.asyncio
async def test_unified_executor_owner_path_denies_without_pin_verified() -> None:
    """UnifiedToolExecutionLayer's Case 1 (owner requester) must deny when
    OwnerAuthContext carries is_owner=False, exactly like a session that
    never passed PIN."""
    unified = UnifiedToolExecutionLayer()
    req = ToolExecutionRequest(
        tool_name="run_shell",
        params={"command": "echo denied"},
        requester="owner",
        session_id=f"s-{uuid.uuid4().hex}",
        correlation_id=f"c-{uuid.uuid4().hex}",
        owner_auth=OwnerAuthContext(is_owner=False, pin_verified=False),
    )
    raw = await unified.execute(req)
    data = json.loads(raw)
    assert "error" in data
    assert "denied" in data["error"].lower() or "pin" in data["error"].lower()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="subprocess pass_fds is not supported on Windows (Wave 4)")
async def test_unified_executor_owner_path_runs_real_command_after_pin() -> None:
    """The same layer, with a genuinely elevated owner context, must run the
    real command and return real stdout/exit_code -- the exact path /shell
    uses in ChatController._handle_shell_command."""
    unified = UnifiedToolExecutionLayer()
    req = ToolExecutionRequest(
        tool_name="run_shell",
        params={"command": "echo real-owner-exec"},
        requester="owner",
        session_id=f"s-{uuid.uuid4().hex}",
        correlation_id=f"c-{uuid.uuid4().hex}",
        owner_auth=OwnerAuthContext(is_owner=True, pin_verified=True),
    )
    raw = await unified.execute(req)
    data = json.loads(raw)
    assert data["success"] is True
    assert data["exit_code"] == 0
    assert "real-owner-exec" in data["output"]


# ── Bare-command allowlist coverage (task-routing bypass for safe reads) ────


@pytest.mark.parametrize("cmd", ["pwd", "ls", "ll", "ps", "df", "free", "who", "w", "whoami"])
def test_bare_shell_allowlist_covers_common_inspection_commands(cmd: str) -> None:
    assert _is_bare_shell_command(cmd) is True


@pytest.mark.parametrize("cmd", ["ps aux", "df -h", "free -m"])
def test_bare_shell_allowlist_covers_commands_with_args(cmd: str) -> None:
    assert _is_bare_shell_command(cmd) is True


def test_bare_shell_allowlist_rejects_arbitrary_text() -> None:
    assert _is_bare_shell_command("install git") is False
    assert _is_bare_shell_command("расскажи анекдот") is False


# ── Provenance: real results are grounded, never fabricated on follow-up ───


@pytest.mark.skipif(sys.platform == "win32", reason="locale-dependent shell error message (Wave 4)")
@pytest.mark.asyncio
async def test_direct_shell_success_is_grounded_for_followup() -> None:
    """A real 'ps aux' result must be persisted so a later conversational
    turn answers from real history instead of guessing."""
    sid = f"cli:test_{uuid.uuid4().hex[:8]}"
    turn_id = f"t-{uuid.uuid4().hex}"
    async with AntigonaBrain() as brain:
        res = await brain.process(
            text="ps aux", user_id="u1", channel="cli", session_id=sid,
            context={"turn_id": turn_id, "correlation_id": turn_id},
        )
        assert res.response_type == ResponseType.CONVERSATION
        assert res.flow_id is None

        msgs = await brain._session_repo.get_messages(sid, limit=20)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[0]["content"] == "ps aux"
    # The persisted assistant turn is the real stdout, not a placeholder.
    assert msgs[1]["content"] == res.text


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason='subprocess pass_fds is not supported on Windows (Wave 4)')
async def test_direct_shell_failure_is_grounded_with_tool_error_tag() -> None:
    """A real failure (e.g. an unresolvable alias) must be persisted with the
    [TOOL_ERROR] marker the persona is instructed to react to honestly."""
    sid = f"cli:test_{uuid.uuid4().hex[:8]}"
    turn_id = f"t-{uuid.uuid4().hex}"
    async with AntigonaBrain() as brain:
        res = await brain.process(
            text="ll", user_id="u1", channel="cli", session_id=sid,
            context={"turn_id": turn_id, "correlation_id": turn_id},
        )
        assert "Ошибка выполнения" in res.text

        msgs = await brain._session_repo.get_messages(sid, limit=20)
    assert msgs[0]["content"] == "ll"
    assert msgs[1]["content"].startswith("[TOOL_ERROR]")
    assert "not found" in msgs[1]["content"]


# ── File tools: real paths, real content, honest NOT_FOUND ─────────────────


@pytest.fixture
def file_unified(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path))
    reg = ToolRegistry()
    register_builtins(reg)
    return UnifiedToolExecutionLayer(registry=reg), tmp_path


@pytest.mark.asyncio
async def test_write_file_without_grant_returns_stable_denial(file_unified) -> None:
    unified, tmp_path = file_unified
    # Target lives under the *real* home directory (derived from the resolver,
    # never the literal /opt/antigona-home) so the write policy classifies it HIGH.
    target = paths.home_dir() / tmp_path.name / "notes.md"
    req = ToolExecutionRequest(
        tool_name="write_file",
        params={"path": str(target), "content": "# Report\nreal content"},
        requester="llm",
        session_id="s1",
        correlation_id="c1",
        turn_id="c1",
    )
    raw = await unified.execute(req)
    data = json.loads(raw)
    assert data == {
        "success": False,
        "error": "HIGH-risk action requires approval grant before execution.",
        "requires_approval": True,
    }
    assert not target.exists()


@pytest.mark.asyncio
async def test_read_file_after_write_matches_real_content(file_unified) -> None:
    unified, tmp_path = file_unified
    target = tmp_path / "roundtrip.txt"
    target.write_text("known content XYZ")

    req = ToolExecutionRequest(
        tool_name="read_file",
        params={"path": str(target)},
        requester="llm",
        session_id="s1",
        correlation_id="c2",
        turn_id="c2",
    )
    raw = await unified.execute(req)
    data = json.loads(raw)
    assert data["success"] is True
    assert data["content"] == "known content XYZ"
    assert data["path"] == str(target.resolve())


@pytest.mark.asyncio
async def test_read_file_missing_returns_honest_not_found(file_unified) -> None:
    unified, tmp_path = file_unified
    missing = tmp_path / "does_not_exist.md"
    req = ToolExecutionRequest(
        tool_name="read_file",
        params={"path": str(missing)},
        requester="llm",
        session_id="s1",
        correlation_id="c3",
        turn_id="c3",
    )
    raw = await unified.execute(req)
    data = json.loads(raw)
    assert "error" in data
    assert "NOT_FOUND" in data["error"]
    assert "success" not in data


@pytest.mark.asyncio
async def test_read_file_rejects_directory_as_not_found(file_unified) -> None:
    unified, tmp_path = file_unified
    req = ToolExecutionRequest(
        tool_name="read_file",
        params={"path": str(tmp_path)},
        requester="llm",
        session_id="s1",
        correlation_id="c4",
        turn_id="c4",
    )
    raw = await unified.execute(req)
    data = json.loads(raw)
    assert "error" in data
    assert "NOT_FOUND" in data["error"]


def test_classify_tool_failure_never_invents_success() -> None:
    """DENIED/FAILED must stay distinct -- classify_tool_failure only ever
    runs on already-failed results and must never claim success."""
    from antigona.result_safety import classify_tool_failure

    failed = ToolResult(ok=False, status="failed", error="tool exited non-zero")
    assert classify_tool_failure(failed) == "tool exited non-zero"


# ── Persona: explicit anti-fabrication instruction is present ──────────────


def test_persona_forbids_fabricating_tool_results() -> None:
    from antigona.context.builder import ContextBuilder

    builder = ContextBuilder()
    messages = builder.build(turn_buffer=None)
    system = messages[0]["content"]
    assert "НЕ ВЫДУМЫВАЙ РЕЗУЛЬТАТЫ ИНСТРУМЕНТОВ" in system
    assert "НИКОГДА не сочиняй вывод команды" in system


def test_integration_tools_advertise_read_file() -> None:
    """The model can only call what's advertised -- read_file must be in the
    real dispatch allowlist DialogueEngine._maybe_run_tool checks against."""
    from antigona.conversation.dialogue_engine import DialogueEngine

    assert "read_file" in DialogueEngine._INTEGRATION_TOOLS
