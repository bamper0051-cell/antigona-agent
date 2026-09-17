"""Wave-green regressions for the owner-reported live defects.

Covers:
  A. TTS must speak the RESOLVED target text, never the raw inbound command.
  B. "покажи текст, который ты озвучивал" returns the voiced text verbatim.
  C. The assistant's own executed actions are visible in its history.
  D. The completion header appears only for a genuinely completed action.
  E. No unsolicited /learn system proposal is pushed into the owner chat.
  F. A "shell:"/"выполни команду" prefix is stripped before dispatch.
  G. No bytecode is written into the immutable code root.
  H. A combined "compose X and voice it" message generates the text FIRST and
     the TTS step speaks the GENERATED text, never the residual fragment.
  I. A bare creative-writing request returns the composed text as a reply and
     is never routed to the file-write path (no "Не удалось определить
     содержимое файла").
  J. A pure retrieval/replay answer carries no action-completion header.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ── A. TTS source resolution ─────────────────────────────────────────────────


@pytest.fixture()
async def brain(tmp_path):
    from antigona.core.brain import AntigonaBrain
    from antigona.sessions.repository import SessionRepository

    repo = SessionRepository(db_path=str(tmp_path / "s.db"))
    b = AntigonaBrain(session_repository=repo, db_path=str(tmp_path / "s.db"))
    await b.connect()
    yield b
    await b.close()


POEM = "Я Антiгона, я живу в тишине,\nИ голос мой звучит в ночной стране."


async def _seed_poem(brain, session_id):
    if not await brain._session_repo.session_exists(session_id):
        await brain._session_repo.create_session(session_id=session_id, title="t")
    await brain._session_repo.add_message(
        session_id=session_id, role="user", content="напиши стих"
    )
    await brain._session_repo.add_message(
        session_id=session_id, role="assistant", content=POEM
    )


@pytest.mark.asyncio
async def test_a_reference_resolves_to_prior_assistant_poem(brain):
    """'озвучь рассказ' after the assistant wrote a poem voices the POEM."""
    await _seed_poem(brain, "cli:u1")
    text, refusal = await brain._resolve_tts_target(
        source_message="озвучь рассказ", candidate="рассказ", session_id="cli:u1"
    )
    assert refusal is None
    assert text == POEM


@pytest.mark.asyncio
async def test_a_explicit_colon_text_is_voiced(brain):
    """'озвучь: <text>' voices <text> — the literal, not the command."""
    payload = "Привет, это тестовый текст."
    text, refusal = await brain._resolve_tts_target(
        source_message=f"озвучь: {payload}", candidate=payload, session_id="cli:u1"
    )
    assert refusal is None
    assert text == payload


@pytest.mark.asyncio
async def test_a_named_workspace_file_is_read_and_voiced(brain, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "story.txt").write_text("Содержимое файла.", encoding="utf-8")
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))
    brain._workspace = str(ws)
    text, refusal = await brain._resolve_tts_target(
        source_message="озвучь story.txt", candidate="story.txt", session_id="cli:u1"
    )
    assert refusal is None
    assert text == "Содержимое файла."


@pytest.mark.asyncio
async def test_a_ambiguous_reference_without_prior_text_refuses(brain):
    text, refusal = await brain._resolve_tts_target(
        source_message="озвучь рассказ", candidate="рассказ", session_id="cli:empty"
    )
    assert text == ""
    assert refusal is not None and "озвучь" in refusal


# ── B. Text retrieval ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b_retrieval_returns_voiced_text(brain):
    brain._last_voiced_text["cli:u1"] = POEM
    resp = await brain._maybe_answer_voiced_text_retrieval(
        "Напиши текст сообщения которое сейчас ты озвучивал", "cli:u1"
    )
    assert resp is not None
    assert resp.text == POEM
    assert "Не удалось определить содержимое" not in resp.text


@pytest.mark.asyncio
async def test_b_show_text_returns_voiced_text(brain):
    brain._last_voiced_text["cli:u1"] = "Короткий текст."
    resp = await brain._maybe_answer_voiced_text_retrieval("покажи текст", "cli:u1")
    assert resp is not None and resp.text == "Короткий текст."


@pytest.mark.asyncio
async def test_b_non_retrieval_request_is_not_intercepted(brain):
    resp = await brain._maybe_answer_voiced_text_retrieval(
        "создай файл note.txt", "cli:u1"
    )
    assert resp is None


# ── C. Self-action visibility ────────────────────────────────────────────────


class _FakeUnifiedExecutor:
    def __init__(self, payload: dict):
        self.payload = payload

    async def execute(self, request):  # noqa: ANN001, ANN201
        return json.dumps(self.payload)


@pytest.mark.asyncio
async def test_c_tts_turn_is_recorded_as_a_visible_action(brain, tmp_path):
    audio = tmp_path / "out.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 64)
    brain._unified_executor = _FakeUnifiedExecutor(
        {"success": True, "data": {"audio_path": str(audio), "size_bytes": 68}}
    )
    brain._last_voiced_text.pop("cli:u1", None)
    await _seed_poem(brain, "cli:u1")

    resp = await brain._execute_tts_intent(
        "рассказ",
        session_id="cli:u1",
        owner_id="owner",
        channel="cli",
        source_message="озвучь рассказ",
    )
    # The POEM is voiced, not the command.
    assert resp.metadata.get("voice_path") == str(audio)
    # The action is recorded so a later turn cannot deny it.
    msgs = await brain._session_repo.get_messages("cli:u1", limit=50)
    action_msgs = [m for m in msgs if str(m.get("content", "")).startswith("[ACTION]")]
    assert action_msgs, "TTS action was not recorded in the conversation history"


# ── D. Completion header ─────────────────────────────────────────────────────


def _render(text, state="SUCCEEDED", completed_action=False):
    from antigona.durable.operation_models import OperationState
    from antigona.events.event_types import FinalResponseReady
    from antigona.presentation.presenter import OperationPresenter

    st = OperationState(state)
    event = FinalResponseReady(
        text=text, terminal_state=state, completed_action=completed_action
    )
    return "".join(OperationPresenter._render_final_chunks(event, st))


def test_d_plain_conversation_has_no_completion_header():
    rendered = _render("Йо")
    assert "Готово" not in rendered
    assert "Йо" in rendered


def test_d_completed_action_has_completion_header():
    assert "Готово" in _render("файл создан", completed_action=True)


def test_d_failure_header_is_honest():
    rendered = _render("boom", state="FAILED")
    assert "Не выполнено" in rendered
    assert "Готово" not in rendered


def test_d_cancelled_header_is_honest():
    rendered = _render("stop", state="CANCELLED")
    assert "Отменено" in rendered
    assert "Готово" not in rendered


# ── E. Affordance leak ───────────────────────────────────────────────────────


def test_e_no_unsolicited_learn_proposal_is_pushed():
    """The presenter must not push a system /learn proposal into the chat."""
    src = (
        Path(__file__).resolve().parents[2]
        / "src/antigona/presentation/presenter.py"
    ).read_text(encoding="utf-8")
    assert "заметила возможное правило" not in src
    assert "/forget" not in src


# ── F. Shell prefix stripping ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("shell: uname -a", "uname -a"),
        ("SHELL:uname -a", "uname -a"),
        ("shell:   ls -la", "ls -la"),
        ("  shell : echo hi  ", "echo hi"),
        ("выполни команду uname -a", "uname -a"),
        ("Выполни команду ls", "ls"),
        ("Выполни pwd", "pwd"),
        ("ls -a", "ls -a"),
    ],
)
def test_f_shell_prefix_is_stripped(raw, expected):
    from antigona.core.brain import _extract_shell_command

    assert _extract_shell_command(raw) == expected


def test_f_non_command_is_not_forced():
    from antigona.core.brain import _extract_shell_command

    assert _extract_shell_command("привет, как дела") is None


# ── G. Bytecode guard ────────────────────────────────────────────────────────


def test_g_sitecustomize_prevents_code_root_bytecode(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    guard = repo_root / "sitecustomize.py"
    assert guard.is_file(), "sitecustomize.py guard is missing from the code root"
    (tmp_path / "sitecustomize.py").write_text(
        guard.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "freshmod.py").write_text("VALUE = 7\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    # The guard is discovered from the code root being on sys.path — exactly
    # the "manual python -m ... run from the code root" scenario.
    env["PYTHONPATH"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", "import freshmod; assert freshmod.VALUE == 7"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    pycache = tmp_path / "__pycache__"
    leaked = list(pycache.glob("freshmod*")) if pycache.is_dir() else []
    assert not leaked, f"bytecode written into the code root: {leaked}"


def test_g_package_import_sets_dont_write_bytecode():
    import antigona

    assert antigona is not None
    assert sys.dont_write_bytecode is True


# ── H. Combined compose + voice (router-level composition) ───────────────────


@pytest.mark.asyncio
async def test_h_combined_message_voices_the_generated_poem(brain, tmp_path):
    """One message that asks for a poem AND its voicing voices the POEM."""
    audio = tmp_path / "poem.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 64)
    captured: dict[str, str] = {}

    class _CapturingExecutor:
        async def execute(self, request):  # noqa: ANN001, ANN201
            captured["text"] = str(request.params.get("text") or "")
            return json.dumps(
                {
                    "success": True,
                    "data": {"audio_path": str(audio), "size_bytes": 68},
                }
            )

    brain._unified_executor = _CapturingExecutor()

    async def _fake_compose(instruction: str, session_id: str) -> str:
        assert "озвучь" not in instruction.casefold()
        return POEM

    brain._compose_text = _fake_compose  # type: ignore[method-assign]
    brain._last_voiced_text.pop("cli:u1", None)

    resp = await brain._maybe_handle_compose_and_voice(
        "Напиши короткое стихотворение про Антiгону и озвучь его голосом",
        "cli:u1",
        owner_id="owner",
        channel="cli",
    )
    assert resp is not None
    # The VOICED text is exactly the generated poem, not the residual fragment.
    assert captured["text"] == POEM
    assert "его голосом" not in captured["text"]
    assert brain._last_voiced_text["cli:u1"] == POEM
    assert POEM in resp.text
    assert resp.metadata.get("voice_path") == str(audio)


@pytest.mark.asyncio
async def test_h_combined_message_through_process(brain, tmp_path):
    """The full process() entry point composes first, then voices."""
    audio = tmp_path / "poem2.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 64)
    captured: dict[str, str] = {}

    class _CapturingExecutor:
        async def execute(self, request):  # noqa: ANN001, ANN201
            captured["text"] = str(request.params.get("text") or "")
            return json.dumps(
                {"success": True, "data": {"audio_path": str(audio), "size_bytes": 68}}
            )

    brain._unified_executor = _CapturingExecutor()

    async def _fake_compose(instruction: str, session_id: str) -> str:
        return POEM

    brain._compose_text = _fake_compose  # type: ignore[method-assign]
    resp = await brain.process(
        "Напиши короткое стихотворение про тишину и озвучь его голосом",
        user_id="u1",
        channel="cli",
    )
    assert captured["text"] == POEM
    assert POEM in resp.text
    assert "его голосом" not in captured["text"]


@pytest.mark.asyncio
async def test_h_residual_voice_fragment_is_never_spoken_as_literal(brain):
    """A dangling voice-noun tail never becomes the payload (fail closed)."""
    text, refusal = await brain._resolve_tts_target(
        source_message="Напиши короткое стихотворение … и озвучь его голосом",
        candidate="его голосом",
        session_id="cli:empty",
    )
    assert text == ""
    assert refusal is not None and "озвучь" in refusal


@pytest.mark.asyncio
async def test_h_residual_voice_fragment_resolves_to_prior_text(brain):
    """With prior assistant text, the residual tail resolves to that text."""
    await _seed_poem(brain, "cli:u1")
    text, refusal = await brain._resolve_tts_target(
        source_message="… и озвучь его голосом",
        candidate="его голосом",
        session_id="cli:u1",
    )
    assert refusal is None
    assert text == POEM


@pytest.mark.asyncio
async def test_h_generation_failure_does_not_voice_anything(brain, tmp_path):
    """If composition fails, the TTS step must not run at all."""
    calls: list[str] = []

    class _CapturingExecutor:
        async def execute(self, request):  # noqa: ANN001, ANN201
            calls.append("executed")
            return json.dumps({"success": True, "data": {}})

    brain._unified_executor = _CapturingExecutor()

    async def _fake_compose(instruction: str, session_id: str) -> str:
        return ""

    brain._compose_text = _fake_compose  # type: ignore[method-assign]
    resp = await brain._maybe_handle_compose_and_voice(
        "Напиши стихотворение и озвучь его голосом", "cli:u1", channel="cli"
    )
    assert resp is not None
    assert calls == []
    assert "озвучка не" in resp.text


# ── I. Creative-writing misrouting ───────────────────────────────────────────


def test_i_bare_creative_request_is_a_chat_answer_not_a_file_write():
    from antigona.router.intent_router import IntentRouter

    router = IntentRouter()
    for text in (
        "Напиши короткое стихотворение про тишину",
        "Напиши четверостишие про море",
        "напиши рассказ о себе",
        "Составь сказку про дракона",
        "Придумай хокку про снег",
    ):
        decision = router.route(text)
        assert decision.intent == "conversation.answer", (text, decision.intent)
        assert decision.reason_code == "creative_compose_request"


def test_i_explicit_file_target_still_writes_a_file():
    from antigona.router.intent_router import IntentRouter

    decision = IntentRouter().route(
        "Напиши стихотворение про тишину и сохрани в файл poem.txt"
    )
    assert decision.intent == "task.file_write"


def test_i_combined_compose_voice_is_not_captured_by_the_text_branch():
    from antigona.router.intent_router import IntentRouter

    decision = IntentRouter().route(
        "Напиши короткое стихотворение про тишину и озвучь его голосом"
    )
    assert decision.intent != "conversation.answer"


@pytest.mark.asyncio
async def test_i_bare_creative_request_returns_text_without_file_error(brain, monkeypatch):
    async def _fake_reply(text, session_id="x", context=None):  # noqa: ANN001, ANN202
        return POEM

    monkeypatch.setattr(brain._dialogue_engine, "reply", _fake_reply)
    resp = await brain.process(
        "Напиши короткое стихотворение про тишину", user_id="u1", channel="cli"
    )
    assert resp.text == POEM
    assert "Не удалось определить содержимое" not in resp.text
    assert resp.response_type != "error"


# ── J. Pure retrieval carries no completion header ───────────────────────────


@pytest.mark.asyncio
async def test_j_retrieval_answer_carries_no_action_completion_outcome(brain):
    brain._last_voiced_text["cli:u1"] = POEM
    resp = await brain._maybe_answer_voiced_text_retrieval(
        "покажи текст, который ты озвучивал", "cli:u1"
    )
    assert resp is not None
    assert resp.text == POEM
    assert (resp.metadata or {}).get("tool_outcome") != "SUCCEEDED"


def test_j_retrieval_reply_renders_without_completion_header():
    rendered = _render(POEM, state="SUCCEEDED", completed_action=False)
    assert "Готово" not in rendered
    assert POEM.splitlines()[0] in rendered
