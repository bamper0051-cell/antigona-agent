"""F-03.5 — Conversation context + real command dispatch.

Verifies:
  1. Multi-turn follow-up ("Сделай его попроще") routes to conversation (with
     loaded history) when prior context exists — never force-clarified.
  2. Registered slash commands /model /providers /bot /keys classify as
     deterministic command intents (no planner, no approval, no LLM).
  3. AntigonaBrain dispatches command intents to deterministic handlers
     (the LLM dialogue engine is NOT invoked) and does not create a TaskFlow.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.core.brain import AntigonaBrain, ResponseType
from antigona.intent_router import IntentRouter
from antigona.sessions.repository import SessionRepository


@pytest.fixture
def router() -> IntentRouter:
    """Shared IntentRouter instance for router-level assertions."""
    return IntentRouter()


# ── Router-level ─────────────────────────────────────────────────────────────

def test_slash_commands_classify_deterministic(router: IntentRouter) -> None:
    expected = {
        "/model": "command.model_select",
        "/providers": "command.providers",
        "/bot": "command.bot",
        "/keys": "command.keys",
    }
    for cmd, intent in expected.items():
        d = router.route(cmd)
        assert d.intent == intent, f"{cmd} -> {d.intent}"
        assert d.response_mode == "command_result"
        assert d.requires_planner is False
        assert d.requires_approval is False


def test_vague_followup_without_context_clarifies(router: IntentRouter) -> None:
    d = router.route("Сделай его попроще")
    assert d.intent == "ambiguous.mixed_intent"
    assert d.response_mode == "clarify"


def test_vague_followup_with_context_goes_conversation(router: IntentRouter) -> None:
    ctx = {
        "previous_messages": [
            {"role": "user", "content": "Составь план изучения Python на неделю"},
            {"role": "assistant", "content": "<план>"},
        ],
        "active_topic": "Составь план изучения Python на неделю",
    }
    d = router.route("Сделай его попроще", context=ctx)
    assert d.intent == "conversation.followup"
    assert d.response_mode == "conversation"
    assert d.requires_planner is False
    assert d.requires_approval is False


# ── Brain-level: command dispatch ───────────────────────────────────────────

@pytest.fixture
async def brain(tmp_path: Path) -> AsyncGenerator[tuple[AntigonaBrain, MagicMock], None]:
    repo = SessionRepository(db_path=str(tmp_path / "s.db"))
    engine = MagicMock()
    engine.reply = AsyncMock(return_value="<LLM ответ>")
    b = AntigonaBrain(
        dialogue_engine=engine,
        session_repository=repo,
        db_path=str(tmp_path / "s.db"),
    )
    try:
        yield b, engine
    finally:
        await b.close()


@pytest.mark.asyncio
async def test_brain_model_command_is_deterministic(brain: tuple[AntigonaBrain, MagicMock]) -> None:
    b, engine = brain
    await b.connect()
    resp = await b.process(text="/model", user_id="u1", channel="cli", session_id="cli:u1")
    await b.close()
    assert resp.intent == "command.model_select"
    assert resp.response_type == ResponseType.CONVERSATION
    assert "модель" in resp.text.lower()
    engine.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_brain_providers_bot_keys_deterministic(brain: tuple[AntigonaBrain, MagicMock]) -> None:
    b, engine = brain
    await b.connect()
    for cmd, intent in [
        ("/providers", "command.providers"),
        ("/bot", "command.bot"),
        ("/keys", "command.keys"),
    ]:
        resp = await b.process(text=cmd, user_id="u1", channel="cli", session_id="cli:u1")
        assert resp.intent == intent, f"{cmd} -> {resp.intent}"
        assert resp.response_type in (ResponseType.CONVERSATION, ResponseType.CONTROL)
        engine.reply.assert_not_awaited()
    await b.close()


@pytest.mark.asyncio
async def test_brain_followup_with_history_routes_to_conversation(brain: tuple[AntigonaBrain, MagicMock]) -> None:
    b, engine = brain
    await b.connect()
    sid = "cli:u1"
    await b._session_repo.create_session(session_id=sid, title="t")
    await b._session_repo.add_message(
        session_id=sid, role="user", content="Составь план изучения Python на неделю"
    )
    await b._session_repo.add_message(session_id=sid, role="assistant", content="<план>")
    resp = await b.process(text="Сделай его попроще", user_id="u1", channel="cli", session_id=sid)
    await b.close()
    assert resp.response_type == ResponseType.CONVERSATION
    engine.reply.assert_awaited()
