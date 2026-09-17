"""General RED regressions for terminal truth and file-draft protocol contamination."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from antigona.channels.telegram.bot import TelegramBot
from antigona.conversation.dialogue_engine import DRAFT_OK, DRAFT_REJECTED, DialogueEngine
from antigona.core.control_plane import FlowStatus
from antigona.durable.operation_models import OperationState


class _TerminalGateway:
    def __init__(self, result: Any = None, *, result_error: Exception | None = None) -> None:
        self.result = result
        self.result_error = result_error

    async def get_flow(self, flow_id: str) -> Any:
        return SimpleNamespace(status=FlowStatus.DONE)

    async def get_result(self, flow_id: str) -> Any:
        if self.result_error is not None:
            raise self.result_error
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "result_error"),
    [
        (SimpleNamespace(terminal=True, success=False, artifacts=[], safe_result_text="failed"), None),
        (SimpleNamespace(terminal=True, success=True, artifacts=[], safe_result_text="no evidence"), None),
        (None, RuntimeError("result unavailable")),
        (SimpleNamespace(terminal=True, success=True, artifacts=[], safe_result_text=None), None),
    ],
    ids=["success-false", "missing-verified-evidence", "result-exception", "missing-safe-result"],
)
async def test_done_without_successful_verified_result_never_publishes_succeeded(
    result: Any, result_error: Exception | None
) -> None:
    bot = TelegramBot(token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    bot.gateway_client = _TerminalGateway(result, result_error=result_error)

    text, state, is_done = await bot._wait_flow_terminal("flow-red", SimpleNamespace(), chat_id=1)

    assert state is not OperationState.SUCCEEDED
    assert is_done is False
    assert "✅ Готово" not in text


@pytest.mark.asyncio
async def test_done_with_successful_verified_result_is_positive_control() -> None:
    artifact = SimpleNamespace(path="receipt.txt", verified=True, sha256="a" * 64, size=3)
    result = SimpleNamespace(
        terminal=True,
        success=True,
        artifacts=[artifact],
        safe_result_text="verified result",
    )
    bot = TelegramBot(token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    bot.gateway_client = _TerminalGateway(result)

    text, state, is_done = await bot._wait_flow_terminal("flow-green-control", SimpleNamespace(), chat_id=1)

    assert (text, state, is_done) == ("verified result", OperationState.SUCCEEDED, True)


class _StubProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def generate(self, messages: list[dict[str, str]], context: Any = None) -> str:
        return self.reply


_REQUEST = "Создай файл receipt.txt с двумя строками:\nRECEIPT TEST\nace279f3"
_LITERAL_BODY = "RECEIPT TEST\nace279f3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol_body",
    [
        '<tool_call><invoke name="workspace.write_text"><content>RECEIPT TEST\nace279f3</content></invoke></tool_call>',
        '<｜DSML｜tool_calls><invoke name="workspace.write"><parameter name="content">RECEIPT TEST\nace279f3</parameter></invoke></｜DSML｜tool_calls>',
        '{"name":"workspace.write_text","arguments":{"content":"RECEIPT TEST\\nace279f3"}}',
        'Создаю файл и сразу читаю обратно для подтверждения.\n\nИнструмент `write_file` выполнен: {"success": true, "path": "C:\\\\Users\\\\user\\\\src\\\\antigona\\\\workspace\\\\receipt.txt", "content": "RECEIPT TEST\\nace279f3", "size_bytes": 20}\n\nИнструмент `read_file` выполнен: {"success": true, "path": "C:\\\\Users\\\\user\\\\src\\\\antigona\\\\workspace\\\\receipt.txt", "content": "RECEIPT TEST\\nace279f3\\n", "size_bytes": 20}\n\nПодтверждаю выполнение:\n1. Полный путь: `C:\\\\Users\\\\user\\\\src\\\\antigona\\\\workspace\\\\receipt.txt`\n2. Точное содержимое файла:\n```\nRECEIPT TEST\nace279f3\n```\nЗадача выполнена.',
    ],
    ids=["xml-tool-call", "dsml-tool-call", "json-function-call", "tool-result-json-narrative"],
)
async def test_file_draft_rejects_tool_protocol_markup_even_when_literals_present(
    tmp_path: Any, protocol_body: str
) -> None:
    engine = DialogueEngine(db_path=str(tmp_path / "draft.db"), provider=_StubProvider(protocol_body))
    try:
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
    finally:
        await engine.close()

    assert draft.status == DRAFT_REJECTED
    assert draft.content is None


@pytest.mark.asyncio
async def test_file_draft_accepts_ordinary_two_line_literal_positive_control(tmp_path: Any) -> None:
    engine = DialogueEngine(db_path=str(tmp_path / "draft.db"), provider=_StubProvider(_LITERAL_BODY))
    try:
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
    finally:
        await engine.close()

    assert draft.status == DRAFT_OK
    assert draft.content == _LITERAL_BODY
