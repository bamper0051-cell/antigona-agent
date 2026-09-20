"""Tests for ActionExecutor CONFIGURE_KEY support.

Covers:
1. parse_action_from_llm extracts CONFIGURE_KEY commands
2. execute runs the full key configuration flow
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from antigona.tools.action_executor import Action, ActionExecutor, ActionType

OWNER_ID = 987654321


def _owner_user_id() -> str:
    """Owner identity for execution tests — matches ANTIGONA_OWNER_ID."""
    return str(OWNER_ID)


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Execution-mechanics tests must not depend on ambient ANTIGONA_PIN."""
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)
    monkeypatch.setenv("ANTIGONA_OWNER_ID", str(OWNER_ID))
    monkeypatch.setenv("ANTIGONA_AUDIT_DB_PATH", str(tmp_path / "test_audit.db"))
    # The only writable root is the canonical workspace (A-CORE-001/A-00): a
    # write outside it is refused, so these mechanics tests write inside it.
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))


class TestConfigureKeyParsing:
    """CONFIGURE_KEY parsing from LLM output."""

    def test_parses_configure_key(self) -> None:
        executor = ActionExecutor()
        text = "CONFIGURE_KEY|openrouter|sk-or-v1-test-key-12345"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.CONFIGURE_KEY
        assert actions[0].provider == "openrouter"
        assert actions[0].key == "sk-or-v1-test-key-12345"

    def test_parses_multiple_actions(self) -> None:
        executor = ActionExecutor()
        text = (
            "WRITE_FILE|/tmp/test.txt|Hello\n"
            "CONFIGURE_KEY|deepseek|sk-" + "abcdef0123456789abcdef0123456789\n"
            "RUN_SHELL|ls -la"
        )
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 3
        # Check order: WRITE_FILE, RUN_SHELL, CONFIGURE_KEY
        # (parsing order in ActionExecutor: WRITE_FILE → SEND_FILE → RUN_SHELL → CONFIGURE_KEY)
        assert actions[0].action_type == ActionType.WRITE_FILE
        assert actions[1].action_type == ActionType.RUN_SHELL
        assert actions[2].action_type == ActionType.CONFIGURE_KEY
        assert actions[2].provider == "deepseek"

    def test_parses_configure_key_with_natural_language(self) -> None:
        executor = ActionExecutor()
        text = """Here is the key for OpenRouter.

CONFIGURE_KEY|openrouter|sk-or-v1-test-key-12345

Please use it for all API calls."""
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.CONFIGURE_KEY

    def test_no_configure_key_in_text(self) -> None:
        executor = ActionExecutor()
        text = "Just a normal message."
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 0

    def test_strip_commands_removes_configure_key(self) -> None:
        executor = ActionExecutor()
        text = "Here is the config\nCONFIGURE_KEY|openrouter|sk-or-key\nDone."
        cleaned = executor.strip_action_commands(text)
        assert "CONFIGURE_KEY" not in cleaned
        assert "Here is the config" in cleaned
        assert "Done." in cleaned


class TestConfigureKeyExecution:
    """CONFIGURE_KEY execution."""

    def test_execute_configure_key_success(self) -> None:
        executor = ActionExecutor()
        action = Action(
            action_type=ActionType.CONFIGURE_KEY,
            provider="openrouter",
            key="sk-or-v1-test-key",
            raw="CONFIGURE_KEY|openrouter|sk-or-v1-test-key",
        )

        mock_result = {
            "success": True,
            "provider": "openrouter",
            "steps": [
                {"step": "write", "success": True, "message": "Key written"},
                {"step": "verify", "success": True, "message": "OK"},
                {"step": "apply", "success": True, "message": "Applied"},
            ],
        }

        with patch(
            "antigona.tools.key_manager.configure_full_keyflow",
            return_value=mock_result,
        ) as configure:
            result = executor.execute(action, user_id=_owner_user_id())
            assert result.success is False
            assert result.action_type == ActionType.CONFIGURE_KEY
            assert result.error == "POLICY_DENIAL"
            assert "requires approval grant" in result.message
            configure.assert_not_called()

    def test_execute_configure_key_failure(self) -> None:
        executor = ActionExecutor()
        action = Action(
            action_type=ActionType.CONFIGURE_KEY,
            provider="openrouter",
            key="sk-or-v1-bad-key",
            raw="CONFIGURE_KEY|openrouter|sk-or-v1-bad-key",
        )

        mock_result = {
            "success": False,
            "provider": "openrouter",
            "steps": [
                {"step": "write", "success": True, "message": "Key written"},
                {"step": "verify", "success": False, "message": "HTTP 401 Unauthorized"},
                {"step": "apply", "success": False, "message": "Skipped"},
            ],
        }

        with patch(
            "antigona.tools.key_manager.configure_full_keyflow",
            return_value=mock_result,
        ) as configure:
            result = executor.execute(action, user_id=_owner_user_id())
            assert result.success is False
            assert result.action_type == ActionType.CONFIGURE_KEY
            assert result.error == "POLICY_DENIAL"
            assert "requires approval grant" in result.message
            configure.assert_not_called()

    def test_execute_all_with_configure_key(self) -> None:
        executor = ActionExecutor()
        actions = [
            Action(
                action_type=ActionType.WRITE_FILE,
                path="test.txt",
                content="hello",
                raw="WRITE_FILE|test.txt|hello",
            ),
            Action(
                action_type=ActionType.CONFIGURE_KEY,
                provider="openrouter",
                key="sk-or-v1-test-key",
                raw="CONFIGURE_KEY|openrouter|sk-or-v1-test-key",
            ),
        ]

        mock_cfk_result = {
            "success": True,
            "provider": "openrouter",
            "steps": [
                {"step": "write", "success": True, "message": "Key written"},
                {"step": "verify", "success": True, "message": "OK"},
                {"step": "apply", "success": True, "message": "Applied"},
            ],
        }

        with patch(
            "antigona.tools.key_manager.configure_full_keyflow",
            return_value=mock_cfk_result,
        ) as configure:
            results = executor.execute_all(actions, user_id=_owner_user_id())
            assert len(results) == 2
            assert results[0].success is True  # WRITE_FILE
            assert results[1].success is False  # CONFIGURE_KEY approval-gated
            assert results[1].error == "POLICY_DENIAL"
            assert "requires approval grant" in results[1].message
            configure.assert_not_called()
