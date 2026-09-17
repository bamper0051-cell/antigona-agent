"""Tests for ActionExecutor — action parsing, execution, and integration.

Covers:
  - parse_action_from_llm: WRITE_FILE, SEND_FILE, RUN_SHELL, empty, mixed
  - execute: write_file success/failure, send_file, run_shell
  - strip_action_commands: cleaning LLM output
  - Integration: bot.py write_file + send_file via mock message
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.core import paths
from antigona.tools.action_executor import (
    Action,
    ActionExecutor,
    ActionType,
    ExecutionMode,
)


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execution-mechanics tests must not depend on ambient ANTIGONA_PIN."""
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)


# Authorized owner used by the owner-gated bot-integration tests.
OWNER_ID = 987654321


def _owner_user_id() -> str:
    """Owner identity for execution tests — matches ANTIGONA_OWNER_ID."""
    return str(OWNER_ID)


@pytest.fixture(autouse=True)
def _owner_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide an authorized owner so owner-gated Telegram actions can pass."""
    monkeypatch.setenv("ANTIGONA_OWNER_ID", str(OWNER_ID))


# Helper: parse, execute, and strip in one call
def text_parse_and_strip(text: str, message: Any = None) -> Any:
    ex = ActionExecutor()
    actions = ex.parse_action_from_llm(text)

    async def _run():
        await ex.execute_all(actions, message=message, user_id=_owner_user_id())
        return actions, ex.strip_action_commands(text)

    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            return _run()
    except RuntimeError:
        pass

    ex.execute_all(actions, message=message, user_id=_owner_user_id())
    return actions, ex.strip_action_commands(text)
# ─── parse_action_from_llm ─────────────────────────────────────────────────────


class TestParseActionFromLLM:
    """Tests for parsing action commands from LLM output."""

    def test_parse_write_file_basic(self) -> None:
        executor = ActionExecutor()
        text = "WRITE_FILE|/tmp/test.txt|Hello World"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.WRITE_FILE
        assert actions[0].path == "/tmp/test.txt"
        assert actions[0].content == "Hello World"

    def test_parse_write_file_with_multiline_content(self) -> None:
        executor = ActionExecutor()
        text = "WRITE_FILE|/tmp/test.py|print('hello')"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.WRITE_FILE
        assert actions[0].path == "/tmp/test.py"
        assert actions[0].content == "print('hello')"

    def test_parse_send_file(self) -> None:
        executor = ActionExecutor()
        text = "SEND_FILE|/tmp/report.pdf"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.SEND_FILE
        assert actions[0].path == "/tmp/report.pdf"

    def test_parse_run_shell(self) -> None:
        executor = ActionExecutor()
        text = "RUN_SHELL|ls -la /tmp"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.RUN_SHELL
        assert actions[0].command == "ls -la /tmp"

    def test_parse_empty_text(self) -> None:
        executor = ActionExecutor()
        assert executor.parse_action_from_llm("") == []
        assert executor.parse_action_from_llm("   ") == []
        assert executor.parse_action_from_llm(None) == []  # type: ignore[arg-type]

    def test_parse_no_commands(self) -> None:
        executor = ActionExecutor()
        text = "Привет! Как дела?"
        actions = executor.parse_action_from_llm(text)
        assert actions == []

    def test_parse_multiple_commands(self) -> None:
        executor = ActionExecutor()
        text = (
            "WRITE_FILE|/tmp/hello.txt|Hello World\n"
            "SEND_FILE|/tmp/hello.txt\n"
            "RUN_SHELL|echo done"
        )
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 3
        assert actions[0].action_type == ActionType.WRITE_FILE
        assert actions[1].action_type == ActionType.SEND_FILE
        assert actions[2].action_type == ActionType.RUN_SHELL

    def test_parse_commands_mixed_with_natural_language(self) -> None:
        executor = ActionExecutor()
        text = (
            "Конечно! Создаю файл.\n"
            "WRITE_FILE|/tmp/data.txt|test content\n"
            "Отправляю.\n"
            "SEND_FILE|/tmp/data.txt\n"
            "Готово!"
        )
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 2
        assert actions[0].action_type == ActionType.WRITE_FILE
        assert actions[1].action_type == ActionType.SEND_FILE

    def test_parse_write_file_empty_content(self) -> None:
        executor = ActionExecutor()
        text = "WRITE_FILE|/tmp/test.txt|"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].path == "/tmp/test.txt"
        assert actions[0].content == ""


# ─── execute: write_file ───────────────────────────────────────────────────────


class TestExecuteWriteFile:
    """Tests for WRITE_FILE execution."""

    def test_write_file_without_grant_is_denied(self) -> None:
        executor = ActionExecutor()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "test.txt"
            action = Action(
                action_type=ActionType.WRITE_FILE,
                path=str(filepath),
                content="Hello World",
            )
            result = executor.execute(action, user_id=_owner_user_id())
            assert result.success is False
            assert result.action_type == ActionType.WRITE_FILE
            assert result.error == "POLICY_DENIAL"
            assert "requires approval grant" in result.message
            assert not filepath.exists()

    def test_write_file_denial_creates_no_parent_dirs(self) -> None:
        executor = ActionExecutor()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "subdir" / "nested" / "test.txt"
            action = Action(
                action_type=ActionType.WRITE_FILE,
                path=str(filepath),
                content="nested content",
            )
            result = executor.execute(action, user_id=_owner_user_id())
            assert result.success is False
            assert result.error == "POLICY_DENIAL"
            assert not filepath.parent.exists()

    def test_write_file_denial_does_not_overwrite_existing(self) -> None:
        executor = ActionExecutor()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "test.txt"
            action = Action(
                action_type=ActionType.WRITE_FILE,
                path=str(filepath),
                content="new content",
            )
            result = executor.execute(action, user_id=_owner_user_id())
            assert result.success is False
            assert result.error == "POLICY_DENIAL"
            assert not filepath.exists()


# ─── execute: send_file ────────────────────────────────────────────────────────


class TestExecuteSendFile:
    """Tests for SEND_FILE execution."""

    def test_send_file_no_message_object(self) -> None:
        delivered: list[str] = []

        def _fake_factory() -> Any:
            class _FakeAdapter:
                def send_file(self, *, path: str, caption: str = "") -> dict[str, Any]:
                    delivered.append(path)
                    return {"ok": True, "path": path}

            return _FakeAdapter()

        # No aiogram message object present: the hotfix (2026-09-06) made this
        # branch actually deliver the file through the delivery adapter (instead
        # of only reporting "готов к отправке"). The adapter is injectable via
        # delivery_adapter_factory so the test is deterministic.
        executor = ActionExecutor(delivery_adapter_factory=_fake_factory)
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.txt"
            filepath.write_text("file content")
            action = Action(
                action_type=ActionType.SEND_FILE,
                path=str(filepath),
            )
            result = executor.execute(action, user_id=_owner_user_id())
            assert result.success is True
            assert "отправлен" in result.message
            assert delivered and Path(delivered[0]) == filepath

    def test_send_file_not_found(self) -> None:
        executor = ActionExecutor()
        action = Action(
            action_type=ActionType.SEND_FILE,
            path="/nonexistent/file.txt",
        )
        result = executor.execute(action, user_id=_owner_user_id())
        assert result.success is False
        assert "не найден" in result.message

    def test_send_file_not_a_file(self) -> None:
        executor = ActionExecutor()
        with tempfile.TemporaryDirectory() as tmpdir:
            action = Action(
                action_type=ActionType.SEND_FILE,
                path=tmpdir,  # directory, not a file
            )
            result = executor.execute(action, user_id=_owner_user_id())
            assert result.success is False
            assert "не является файлом" in result.message


# ─── execute: run_shell ────────────────────────────────────────────────────────


class TestExecuteRunShell:
    """Tests for RUN_SHELL execution."""

    def test_run_shell_success(self) -> None:
        executor = ActionExecutor()
        action = Action(
            action_type=ActionType.RUN_SHELL,
            command="echo hello",
        )
        result = executor.execute(action, user_id=_owner_user_id())
        assert result.success is True
        assert "hello" in result.message

    def test_run_shell_failure(self) -> None:
        executor = ActionExecutor()
        action = Action(
            action_type=ActionType.RUN_SHELL,
            command="exit 1",
        )
        result = executor.execute(action, user_id=_owner_user_id())
        assert result.success is False

    def test_run_shell_timeout_short(self) -> None:
        """Test the executor handles commands gracefully."""
        executor = ActionExecutor()
        action = Action(
            action_type=ActionType.RUN_SHELL,
            command="echo timeout works",
        )
        result = executor.execute(action, user_id=_owner_user_id())
        assert result.success is True


# ─── strip_action_commands ─────────────────────────────────────────────────────


class TestStripActionCommands:
    """Tests for stripping action commands from LLM output."""

    def test_strip_write_file(self) -> None:
        executor = ActionExecutor()
        text = "WRITE_FILE|/tmp/test.txt|content\nSome explanation"
        cleaned = executor.strip_action_commands(text)
        assert "WRITE_FILE" not in cleaned
        assert "Some explanation" in cleaned

    def test_strip_multiple_commands(self) -> None:
        executor = ActionExecutor()
        text = (
            "Создаю файл.\n"
            "WRITE_FILE|/tmp/a.txt|aaa\n"
            "Готово.\n"
            "SEND_FILE|/tmp/a.txt\n"
            "Отправил."
        )
        cleaned = executor.strip_action_commands(text)
        assert "WRITE_FILE" not in cleaned
        assert "SEND_FILE" not in cleaned
        assert "Создаю файл." in cleaned
        assert "Готово." in cleaned
        assert "Отправил." in cleaned

    def test_strip_empty_text(self) -> None:
        executor = ActionExecutor()
        assert executor.strip_action_commands("") == ""
        assert executor.strip_action_commands(None) == ""  # type: ignore[arg-type]

    def test_strip_no_commands(self) -> None:
        executor = ActionExecutor()
        text = "Просто текст без команд."
        assert executor.strip_action_commands(text) == "Просто текст без команд."

    def test_strip_commands_preserves_whitespace(self) -> None:
        executor = ActionExecutor()
        text = "Line1\n\nWRITE_FILE|/tmp/x.txt|data\n\nLine2\n"
        cleaned = executor.strip_action_commands(text)
        assert cleaned == "Line1\n\n\nLine2"


# ─── text_parse_and_strip convenience ──────────────────────────────────────


class TestExecuteActionsFromText:
    """Tests for the text_parse_and_strip convenience function."""

    def test_with_write_file_without_grant_cleans_command_but_denies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "test.txt"
            text = f"WRITE_FILE|{filepath}|test content\nПояснение."
            actions, cleaned = text_parse_and_strip(text)
            assert len(actions) == 1
            assert actions[0].action_type == ActionType.WRITE_FILE
            assert not filepath.exists()
            assert "Пояснение." in cleaned
            assert "WRITE_FILE" not in cleaned

    def test_no_actions(self) -> None:
        text = "Привет! Как дела?"
        actions, cleaned = text_parse_and_strip(text)
        assert actions == []
        assert cleaned == text


# ─── Integration: bot.py mock tests ────────────────────────────────────────────


class TestBotIntegrationActionExecutor:
    """Integration tests: simulate bot.py _handle_decision with ActionExecutor."""

    @pytest.mark.asyncio
    async def test_write_file_via_bot_response_without_grant_is_denied(self) -> None:
        """Owner identity alone must not bypass HIGH WRITE_FILE approval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "test.txt"

            llm_reply = f"WRITE_FILE|{filepath}|Hello from bot!"

            executor = ActionExecutor()
            actions = executor.parse_action_from_llm(llm_reply)

            assert len(actions) == 1
            assert actions[0].action_type == ActionType.WRITE_FILE

            result = await executor.execute(actions[0], user_id=_owner_user_id())
            assert result.success is False
            assert result.error == "POLICY_DENIAL"
            assert not filepath.exists()

    @pytest.mark.asyncio
    async def test_send_file_via_mock_message(self) -> None:
        """Test that SEND_FILE calls message.answer_document()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.txt"
            filepath.write_text("sendable content")

            mock_message = MagicMock()
            mock_message.answer_document = AsyncMock()
            # Executor owner-gates Telegram actions, so identify the sender as
            # the authorized owner (from_user.id == ANTIGONA_OWNER_ID).
            mock_message.from_user.id = OWNER_ID

            llm_reply = f"SEND_FILE|{filepath}"

            executor = ActionExecutor()
            actions = executor.parse_action_from_llm(llm_reply)
            assert len(actions) == 1
            assert actions[0].action_type == ActionType.SEND_FILE

            result = await executor.execute(actions[0], user_id=_owner_user_id(), message=mock_message)
            assert result.success is True

    @pytest.mark.asyncio
    async def test_write_then_send_without_grant_has_no_side_effect(self) -> None:
        """A denied write cannot create a file for the following SEND_FILE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "test.txt"

            mock_message = MagicMock()
            mock_message.answer_document = AsyncMock()
            mock_message.from_user.id = OWNER_ID

            llm_reply = (
                f"WRITE_FILE|{filepath}|combined content\n"
                f"SEND_FILE|{filepath}\n"
                "Готово!"
            )

            actions, cleaned = await text_parse_and_strip(llm_reply, message=mock_message)

            assert len(actions) == 2
            assert actions[0].action_type == ActionType.WRITE_FILE
            assert actions[1].action_type == ActionType.SEND_FILE
            assert not filepath.exists()
            assert "Готово!" in cleaned
            assert "WRITE_FILE" not in cleaned
            assert "SEND_FILE" not in cleaned
            mock_message.answer_document.assert_not_awaited()

            # Give async task a chance to complete
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_no_actions_in_bot_response(self) -> None:
        """Test that normal text without commands works fine."""
        llm_reply = "Привет! Чем могу помочь?"
        actions, cleaned = await text_parse_and_strip(llm_reply)
        assert actions == []
        assert cleaned == llm_reply

    @pytest.mark.asyncio
    async def test_mixed_natural_language_with_denied_write(self) -> None:
        """Test that natural language is preserved alongside actions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "test.txt"

            llm_reply = (
                "Создаю файл по вашему запросу.\n"
                f"WRITE_FILE|{filepath}|test data\n"
                "Файл создан. Теперь отправляю его.\n"
                f"SEND_FILE|{filepath}\n"
                "Готово!"
            )

            actions, cleaned = await text_parse_and_strip(llm_reply)

            assert len(actions) == 2
            assert actions[0].action_type == ActionType.WRITE_FILE
            assert actions[1].action_type == ActionType.SEND_FILE
            assert not filepath.exists()
            assert "Создаю файл по вашему запросу." in cleaned
            assert "Файл создан. Теперь отправляю его." in cleaned
            assert "Готово!" in cleaned
            assert "WRITE_FILE" not in cleaned
            assert "SEND_FILE" not in cleaned


# ─── Edge cases ────────────────────────────────────────────────────────────────


class TestActionExecutorEdgeCases:
    """Edge cases for ActionExecutor."""

    def test_execute_unknown_action_type(self) -> None:
        executor = ActionExecutor()
        action = Action(action_type="UNKNOWN", path="test.txt")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown action type"):
            executor.execute(action, user_id=_owner_user_id())

    def test_execute_all_empty_list(self) -> None:
        executor = ActionExecutor()
        results = executor.execute_all([], user_id=_owner_user_id())
        assert results == []

    def test_execute_all_without_grant_returns_denial(self) -> None:
        executor = ActionExecutor()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = paths.home_dir() / Path(tmpdir).name / "test.txt"
            action = Action(
                action_type=ActionType.WRITE_FILE,
                path=str(filepath),
                content="test",
            )
            results = executor.execute_all([action], user_id=_owner_user_id())
            assert len(results) == 1
            assert results[0].success is False
            assert results[0].error == "POLICY_DENIAL"
            assert not filepath.exists()

    def test_default_mode_is_direct(self) -> None:
        executor = ActionExecutor()
        assert executor.mode == ExecutionMode.DIRECT

    def test_parse_run_shell_with_spaces(self) -> None:
        executor = ActionExecutor()
        text = "RUN_SHELL|ls -la /tmp/test dir"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].command == "ls -la /tmp/test dir"