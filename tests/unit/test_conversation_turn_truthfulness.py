"""Conversation-path truthfulness: a turn's outcome belongs to THAT turn only.

Live finding (2026-09-10): a plain conversation turn was answered with a stale
fencing/ownership error that had been recorded by an EARLIER turn, even though
no tool executed in the current turn.  Two independent leaks were closed:

* ``DialogueEngine._last_tool_outcome`` / ``_last_tool_error`` persisted across
  turns and were only reset inside ``_maybe_run_tool`` — a turn that ran no tool
  inherited the previous turn's FAILED/DENIED outcome, which the client then
  rendered as a fresh failure.
* the model could echo a previous turn's recorded failure text back as the
  current answer.

Contract under test:
  * the current turn's outcome is derived ONLY from the current turn;
  * a turn with no tool call has NO outcome (never a stale failure);
  * a reply that merely replays a previous turn's failure, or that carries
    fail-closed internals, is never presented as the current answer — a neutral
    answer is given instead.
"""
from __future__ import annotations

import pytest

from antigona.context.builder import ContextBuilder
from antigona.conversation.dialogue_engine import (
    _NEUTRAL_NO_ACTION_REPLY,
    DialogueEngine,
    _is_stale_error_replay,
    _prior_tool_error_texts,
)
from antigona.core.brain import AntigonaBrain
from antigona.sessions.repository import SessionRepository

SESSION = "telegram:12345"
FENCING_ERROR = (
    "ownership enabled but write surface 'sandbox.shell' has no fencing token; "
    "denying before any mutation (fail-closed)"
)
FAILURE_HISTORY = f"Команда 'ls' завершилась ошибкой: {FENCING_ERROR}"


class _Provider:
    """Deterministic provider returning a scripted reply per call."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def generate(self, messages):  # noqa: ANN001 - test double
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


async def _seeded_repo(tmp_path, history: list[tuple[str, str]]):
    repo = SessionRepository(db_path=str(tmp_path / "sessions.db"))
    await repo.connect()
    await repo.create_session(session_id=SESSION, title="t")
    for role, content in history:
        await repo.add_message(session_id=SESSION, role=role, content=content)
    return repo


def _engine(tmp_path, repo, provider, *, registry=None) -> DialogueEngine:
    return DialogueEngine(
        repository=repo,
        provider=provider,
        context_builder=ContextBuilder(memory_dir=str(tmp_path / "mem")),
        registry=registry,
    )


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_stale_replay_detector_matches_a_recorded_failure_only() -> None:
    history = [
        {"role": "user", "content": "выполни ls"},
        {"role": "assistant", "content": "[TOOL_ERROR] " + FAILURE_HISTORY},
    ]
    errors = _prior_tool_error_texts(history)
    assert errors == [FAILURE_HISTORY]

    # verbatim echo of the previous failure -> stale replay
    assert _is_stale_error_replay(FAILURE_HISTORY, errors) is True
    # a truncated echo (the error is contained in the reply) -> stale replay
    assert _is_stale_error_replay(FENCING_ERROR, errors) is True
    # an ordinary answer is never flagged
    assert _is_stale_error_replay("Привет! Чем могу помочь?", errors) is False
    assert _is_stale_error_replay("", errors) is False


def test_prior_errors_only_from_assistant_failure_grounding() -> None:
    history = [
        {"role": "assistant", "content": "обычный ответ"},
        {"role": "user", "content": "[TOOL_ERROR] не должно учитываться"},
    ]
    assert _prior_tool_error_texts(history) == []


# ── 1. stale-error replay is never the current turn's answer ─────────────────


@pytest.mark.asyncio
async def test_stale_error_replay_not_returned_as_current_answer(tmp_path) -> None:
    repo = await _seeded_repo(
        tmp_path,
        [("user", "выполни ls"), ("assistant", "[TOOL_ERROR] " + FAILURE_HISTORY)],
    )
    # The model echoes the previous turn's failure text verbatim.
    provider = _Provider([FAILURE_HISTORY])
    engine = _engine(tmp_path, repo, provider)
    try:
        reply = await engine.reply("привет", session_id=SESSION)
    finally:
        await engine.close()

    assert reply == _NEUTRAL_NO_ACTION_REPLY
    assert "fencing token" not in reply.lower()
    assert "ownership" not in reply.lower()
    # A no-tool turn must not carry an outcome at all.
    assert engine._last_tool_outcome is None
    assert engine._last_tool_error is None


@pytest.mark.asyncio
async def test_no_tool_echo_of_fail_closed_internals_is_neutralised(tmp_path) -> None:
    """A no-tool reply carrying fail-closed internals is never shown verbatim."""
    repo = await _seeded_repo(tmp_path, [("user", "привет")])
    provider = _Provider([f"Всё сломалось: {FENCING_ERROR}"])
    engine = _engine(tmp_path, repo, provider)
    try:
        reply = await engine.reply("как дела?", session_id=SESSION)
    finally:
        await engine.close()
    assert reply == _NEUTRAL_NO_ACTION_REPLY
    assert "fail-closed" not in reply.lower()


# ── 2. multi-turn history containing prior failures stays honest ─────────────


@pytest.mark.asyncio
async def test_multi_turn_history_with_prior_failures_keeps_normal_answer(
    tmp_path,
) -> None:
    repo = await _seeded_repo(
        tmp_path,
        [
            ("user", "выполни ls"),
            ("assistant", "[TOOL_ERROR] " + FAILURE_HISTORY),
            ("user", "выполни pwd"),
            ("assistant", "[TOOL_ERROR] " + FAILURE_HISTORY),
        ],
    )
    provider = _Provider(["Привет! Чем помочь?"])
    engine = _engine(tmp_path, repo, provider)
    try:
        reply = await engine.reply("привет", session_id=SESSION)
    finally:
        await engine.close()

    # A genuine (non-echoing) reply is preserved, and no stale outcome leaks.
    assert reply == "Привет! Чем помочь?"
    assert engine._last_tool_outcome is None
    assert engine._last_tool_error is None


# ── 3. a turn with NO tool call after a failure turn reports no outcome ─────


@pytest.mark.asyncio
async def test_no_tool_turn_after_failure_turn_has_no_outcome(tmp_path) -> None:
    repo = await _seeded_repo(tmp_path, [("user", "старт")])
    provider = _Provider(['Раз: ⟪' + 'tool:evil_tool x="1"⟫', "Привет!"])
    engine = _engine(tmp_path, repo, provider, registry=object())
    try:
        # Turn 1: model asks for a tool that is not advertised -> DENIED.
        first = await engine.reply("сделай что-нибудь", session_id=SESSION)
        assert engine._last_tool_outcome == "DENIED"
        assert engine._last_tool_error
        assert "недоступен" in first

        # Turn 2: no tool call at all -> outcome must be cleared, not inherited.
        second = await engine.reply("привет", session_id=SESSION)
        assert engine._last_tool_outcome is None
        assert engine._last_tool_error is None
        assert second == "Привет!"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_brain_metadata_has_no_stale_outcome_after_failure_turn(
    tmp_path,
) -> None:
    """End-to-end at the brain boundary: turn 2 metadata carries no outcome."""
    repo = await _seeded_repo(tmp_path, [("user", "старт")])
    provider = _Provider(['Раз: ⟪' + 'tool:evil_tool x="1"⟫', "Привет!"])
    engine = _engine(tmp_path, repo, provider, registry=object())
    brain = AntigonaBrain(
        dialogue_engine=engine,
        session_repository=repo,
        db_path=str(tmp_path / "s.db"),
    )
    first = await brain._handle_conversation("сделай что-нибудь", SESSION, None)
    assert first.metadata.get("tool_outcome") == "DENIED"

    second = await brain._handle_conversation("привет", SESSION, None)
    assert "tool_outcome" not in second.metadata
    assert "last_error" not in second.metadata


@pytest.mark.asyncio
async def test_empty_turn_reports_no_outcome(tmp_path) -> None:
    repo = await _seeded_repo(tmp_path, [("user", "x")])
    engine = _engine(tmp_path, repo, _Provider(["never used"]))
    try:
        assert await engine.reply("   ", session_id=SESSION) == "..."
        assert engine._last_tool_outcome is None
    finally:
        await engine.close()


# ── 4. bot-side guard: no-tool conversation turn cannot show a stale failure ──


def test_bot_neutralises_stale_failure_marker() -> None:
    from antigona.channels.telegram.bot import (
        _NEUTRAL_STALE_ERROR_REPLY,
        _contains_stale_error_marker,
    )

    assert _contains_stale_error_marker(f"Ошибка: {FENCING_ERROR}") is True
    assert _contains_stale_error_marker("Обычный дружелюбный ответ") is False
    # A bare "ownership" in ordinary prose must NOT be treated as an error.
    assert _contains_stale_error_marker("обсудим ownership проекта") is False
    assert "ничего не выполняла" in _NEUTRAL_STALE_ERROR_REPLY
