"""Regression: reasoning models can return HTTP 200 with empty content.

DeepSeek-v4-flash (and similar) spend the entire ``max_tokens`` budget on
``reasoning_content``. ``message.content`` is then ``""`` and
``finish_reason`` is ``"length"``. ``draft_file_content`` used to treat that
as a real model failure and surface the fallback-echo
«Не удалось определить содержимое файла...».

These tests lock two contracts:

1. ``OpenAICompatibleProvider.generate`` retries with a larger ``max_tokens``
   when the first reply is empty *because* reasoning ate the budget.
2. ``draft_file_content_result`` requests enough ``max_tokens`` that a
   reasoning-style stub (empty below 200 tokens, content above) succeeds.
3. Real provider errors (401 / timeout) stay degraded, not a silent retry-as-ok.
"""

from __future__ import annotations

import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from antigona.conversation.dialogue_engine import (
    DRAFT_MAX_TOKENS,
    DRAFT_OK,
    DRAFT_UNAVAILABLE,
    DialogueEngine,
)
from antigona.providers.base import ProviderError
from antigona.providers.openai_compatible import OpenAICompatibleProvider

_REQUEST = "Создай файл story.txt и запиши туда одну строку: ГОТОВО"


def _chat_response(
    *,
    content: str | None,
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
    status_code: int = 200,
    text: str = "",
) -> MagicMock:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.text = text
    message: dict[str, Any] = {"content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    mock_resp.json.return_value = {
        "choices": [{"message": message, "finish_reason": finish_reason}]
    }
    return mock_resp


class _ReasoningBudgetStub:
    """Mimic DeepSeek-v4-flash: empty content unless max_tokens is large enough."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any] | None] = []

    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append(context)
        max_tokens = int((context or {}).get("max_tokens", 16))
        if max_tokens < 200:
            return ""
        return "ГОТОВО"


class _AuthFailingProvider:
    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        raise ProviderError("API returned 401: invalid api key")


class _TimeoutProvider:
    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        raise ProviderError("HTTP request failed: timed out")


def test_generate_retries_when_reasoning_consumes_token_budget() -> None:
    """Small max_tokens + reasoning_content + finish_reason=length → retry."""
    provider = OpenAICompatibleProvider(api_key="key", model="deepseek-v4-flash")
    first = _chat_response(
        content="",
        finish_reason="length",
        reasoning_content="thinking about the file contents" * 3,
    )
    second = _chat_response(content="ГОТОВО", finish_reason="stop")

    with patch.object(httpx.Client, "post", side_effect=[first, second]) as mock_post:
        reply = provider.generate(
            [{"role": "user", "content": _REQUEST}],
            context={"max_tokens": 16},
        )

    assert reply == "ГОТОВО"
    assert mock_post.call_count == 2
    first_tokens = mock_post.call_args_list[0][1]["json"]["max_tokens"]
    retry_tokens = mock_post.call_args_list[1][1]["json"]["max_tokens"]
    assert first_tokens == 16
    assert retry_tokens >= 4096
    assert retry_tokens > first_tokens


def test_generate_does_not_retry_true_empty_stop() -> None:
    """Empty content with finish_reason=stop is a real empty reply, not a budget miss."""
    provider = OpenAICompatibleProvider(api_key="key")
    empty_stop = _chat_response(content="", finish_reason="stop")

    with patch.object(httpx.Client, "post", return_value=empty_stop) as mock_post:
        reply = provider.generate([{"role": "user", "content": "hi"}])

    assert reply == ""
    assert mock_post.call_count == 1


def test_generate_does_not_retry_http_401() -> None:
    provider = OpenAICompatibleProvider(api_key="bad-key")
    unauthorized = _chat_response(
        content="",
        status_code=401,
        text="invalid api key",
    )

    with patch.object(httpx.Client, "post", return_value=unauthorized) as mock_post:
        with pytest.raises(ProviderError, match="401"):
            provider.generate([{"role": "user", "content": "hi"}])

    assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_draft_requests_enough_tokens_for_reasoning_model() -> None:
    """draft_file_content must not call generate() with a tiny/absent max_tokens."""
    stub = _ReasoningBudgetStub()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=stub)
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
        await engine.repository.close()

    assert stub.calls, "generate must be called"
    context = stub.calls[0] or {}
    assert int(context.get("max_tokens", 0)) == DRAFT_MAX_TOKENS
    assert DRAFT_MAX_TOKENS >= 200
    assert draft.content == "ГОТОВО"
    assert draft.status == DRAFT_OK


@pytest.mark.asyncio
async def test_draft_http_401_stays_unavailable() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=_AuthFailingProvider())
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
        await engine.repository.close()

    assert draft.content is None
    assert draft.status == DRAFT_UNAVAILABLE


@pytest.mark.asyncio
async def test_draft_timeout_stays_unavailable() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=_TimeoutProvider())
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
        await engine.repository.close()

    assert draft.content is None
    assert draft.status == DRAFT_UNAVAILABLE
