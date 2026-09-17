"""P1 Phase 7: auth handler unit tests (restored from pyc contract after rollback)."""
from __future__ import annotations

from typing import Any

import pytest

from antigona.channels.telegram import auth_handlers
from antigona.tools import pin_gate


class _DummyUser:
    id: int

    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _DummyChat:
    id: int

    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _DummyMessage:
    chat: _DummyChat
    from_user: _DummyUser
    text: str
    answers: list[str]
    delete_fails: bool

    def __init__(self, chat_id: int, user_id: int, text: str, delete_fails: bool = False) -> None:
        self.chat = _DummyChat(chat_id)
        self.from_user = _DummyUser(user_id)
        self.text = text
        self.answers = []
        self.delete_fails = delete_fails

    async def answer(self, text: str) -> None:
        self.answers.append(text)

    async def delete(self) -> None:
        if self.delete_fails:
            raise RuntimeError("delete failed")


@pytest.fixture(autouse=True)
def _isolate_pin_gate_elevation(tmp_path):
    """Wave B2: pin_gate elevation/lockout state is the durable ElevationAuthority.
    Give each test its own store instead of clearing module dicts that no longer
    exist. Security assertions in the tests are unchanged."""
    from antigona.security.elevation import ElevationAuthority

    pin_gate.set_elevation_authority(ElevationAuthority(db_path=tmp_path / "elev.db"))
    yield
    pin_gate.set_elevation_authority(None)


def _reset_pin_gate_state() -> None:
    pin_gate.reset_all_sessions()  # B2: clears sessions + lockout + confirmations


def _dummy(chat_id: int, user_id: int, text: str, delete_fails: bool = False) -> Any:
    """Build a structural stand-in for aiogram Message (duck-typed by handlers)."""
    return _DummyMessage(chat_id, user_id, text, delete_fails)


@pytest.mark.anyio
async def test_cmd_unlock_non_owner_blocked_before_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G2: owner identity is checked BEFORE the PIN."""
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    _reset_pin_gate_state()

    message = _dummy(955_111, 999, "/unlock 1234")
    result = await auth_handlers.cmd_unlock(message)

    assert "Доступ запрещён" in message.answers[0]
    assert result is None


@pytest.mark.anyio
async def test_cmd_unlock_wrong_pin_counts_attempt_and_delete_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    _reset_pin_gate_state()

    # Best-effort delete must not break the command even if it fails.
    message = _dummy(955_111, 42, "/unlock 0000", delete_fails=True)
    result = await auth_handlers.cmd_unlock(message)

    assert "Неверный PIN" in message.answers[0]
    assert result is None
    assert pin_gate.is_elevated(955_111) is False


@pytest.mark.anyio
async def test_cmd_unlock_correct_pin_elevates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    _reset_pin_gate_state()

    message = _dummy(955_111, 42, "/unlock 1234")
    result = await auth_handlers.cmd_unlock(message)

    assert result == "UNLOCKED"
    assert "Режим владельца" in message.answers[0]
    assert pin_gate.is_elevated(955_111) is True
    info = pin_gate.get_session_info(955_111)
    assert info is not None
    assert info["user_id"] == 42


@pytest.mark.anyio
async def test_cmd_unlock_pin_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)
    _reset_pin_gate_state()

    message = _dummy(955_111, 42, "/unlock 1234")
    result = await auth_handlers.cmd_unlock(message)

    assert "PIN не настроен" in message.answers[0]
    assert result is None


@pytest.mark.anyio
async def test_cmd_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    _reset_pin_gate_state()

    pin_gate.elevate_session(955_111, 42)
    assert pin_gate.is_elevated(955_111) is True

    message = _dummy(955_111, 42, "/lock")
    result = await auth_handlers.cmd_lock(message)

    assert result == "LOCKED"
    assert "Режим владельца отключён" in message.answers[0]
    assert pin_gate.is_elevated(955_111) is False


@pytest.mark.anyio
async def test_cmd_auth_status_non_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    _reset_pin_gate_state()

    message = _dummy(955_111, 999, "/auth_status")
    result = await auth_handlers.cmd_auth_status(message)

    assert "Доступ запрещён" in message.answers[0]
    assert result is None


@pytest.mark.anyio
async def test_cmd_auth_status_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    _reset_pin_gate_state()

    message = _dummy(955_111, 42, "/auth_status")
    result = await auth_handlers.cmd_auth_status(message)

    assert "Владелец: подтверждён" in message.answers[0]
    assert result is None


@pytest.mark.anyio
async def test_cmd_confirm_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    _reset_pin_gate_state()

    message = _dummy(955_111, 42, "/confirm invalid-token")
    result = await auth_handlers.cmd_confirm(message)

    assert "Неверный или истёкший" in message.answers[0]
    assert result is None
