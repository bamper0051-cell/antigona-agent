"""Regressions: phantom TTS/MCP capability, error-reason laundering, aliases.

Owner-visible defect: an approved TTS request produced a bare
"Задача не выполнена." because the brain routed "озвучь ..." to an
UNREGISTERED MCP server (edge-tts) and the real failure reason was laundered
away at three layers (bridge -> orchestrator -> user template).
"""

from __future__ import annotations

import pytest

from antigona.core import mcp as mcp_module
from antigona.core.brain import _parse_mcp_request, _validate_mcp_server
from antigona.engine.unified_executor import canonical_tool_name
from antigona.result_safety import public_failure_reason

# Exact owner message (owner Telegram view id 226604) that triggered the
# phantom flow: the brain parsed the TTS verb, created an "mcp" flow against
# the unregistered edge-tts server, and the owner got "Задача не выполнена.".
OWNER_PHANTOM_TEXT = (
    "Расскажи историю о том Кто ты такая что ты умеешь на что-то способны "
    "какие инструменты У тебя есть и этот текст необходимо озвучить"
)


# ── Phantom capability: nothing advertises an unregistered server ────────────


def test_owner_phantom_text_no_longer_routes_to_unregistered_mcp():
    parsed = _parse_mcp_request(OWNER_PHANTOM_TEXT)
    assert parsed is not None
    # A REAL TTS request resolved to the working contract tool...
    assert parsed["kind"] == "tts"
    assert parsed["tool"] == "speech.tts"
    # ...and never to the phantom MCP server.
    assert parsed["server"] != "edge-tts"
    assert parsed["arguments"]["text"].strip()


@pytest.mark.parametrize(
    "text",
    [
        "озвучка не работает",
        "почему ты не озвучил рассказ",
        "ты не озвучил мой текст",
        "расскажи про озвучку",
        "объясни, как работает озвучка",
        "какие инструменты для озвучки у тебя есть?",
        "умеешь ли ты озвучивать текст",
        "не нужно озвучивать",
        "что такое tts",
    ],
)
def test_meta_or_negated_tts_mention_is_not_an_execution_request(text):
    assert _parse_mcp_request(text) is None


@pytest.mark.parametrize(
    "text,expected_text",
    [
        ("озвучь рассказ о себе", "рассказ о себе"),
        ("произнеси доброе утро", "доброе утро"),
    ],
)
def test_imperative_tts_request_resolves_to_speech_tts(text, expected_text):
    parsed = _parse_mcp_request(text)
    assert parsed is not None and parsed["kind"] == "tts"
    assert parsed["tool"] == "speech.tts"
    assert parsed["arguments"]["text"] == expected_text


def test_topical_mcp_mention_does_not_block_a_tts_request():
    # "MCP-сервер" is a topical mention, NOT an mcp invocation: it must not
    # swallow a legitimate TTS request (found live).
    parsed = _parse_mcp_request(
        "озвучь: Проверка озвучки Антигоны после удаления фантомного MCP-сервера."
    )
    assert parsed is not None and parsed["kind"] == "tts"
    assert parsed["tool"] == "speech.tts"


def test_incomplete_mcp_request_invents_no_default_server():
    assert _parse_mcp_request("вызови mcp tool=speak") is None
    assert _parse_mcp_request("вызови mcp server=edge-tts") is None


# ── Fail fast: registration validated BEFORE a durable flow is created ───────


def test_explicit_mcp_call_to_unregistered_server_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    parsed = _parse_mcp_request('вызови mcp server="edge-tts" tool="speak"')
    assert parsed is not None and parsed["kind"] == "mcp"
    rejection = _validate_mcp_server(parsed["server"])
    assert rejection is not None
    assert "не зарегистрирован" in rejection
    # actionable: names the known servers so the owner can act
    assert "context7" in rejection
    assert "speech.tts" in rejection


def test_validate_mcp_server_accepts_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    # a fresh registry seeds context7
    assert _validate_mcp_server("context7") is None


# ── Tool-name aliases: canonical names + safe aliases ────────────────────────


@pytest.mark.parametrize(
    "emitted,canonical",
    [
        ("sandbox_shell", "sandbox.shell"),
        ("sandbox-shell", "sandbox.shell"),
        ("sandbox.shell", "sandbox.shell"),
        ("speech_tts", "speech.tts"),
        ("text_to_speech", "speech.tts"),
        ("speech.tts", "speech.tts"),
        ("workspace.write_text", "workspace.write_text"),
        ("run_shell", "run_shell"),
    ],
)
def test_canonical_tool_name_aliases(emitted, canonical):
    assert canonical_tool_name(emitted) == canonical


def test_alias_never_escalates_to_the_host_shell():
    # the sandbox alias must NOT resolve to the exempt host `run_shell` path
    assert canonical_tool_name("sandbox_shell") != "run_shell"


# ── Error-reason laundering: concrete reason, never internals/exception text ─


def test_public_failure_reason_is_specific():
    reason = public_failure_reason(
        "mcp server 'edge-tts' is not registered (registered: context7)"
    )
    assert reason
    assert "not registered" in reason


def test_public_failure_reason_refuses_raw_exception_text():
    assert public_failure_reason("RuntimeError: database password=hunter2") == ""
    assert public_failure_reason("Traceback (most recent call last):") == ""


def test_public_failure_reason_redacts_secrets():
    reason = public_failure_reason("tool failed: password=hunter2")
    assert "hunter2" not in reason


def test_public_failure_reason_hides_internal_security_wording():
    neutral = public_failure_reason(
        "protected execution denied: ownership enabled but write surface "
        "'sandbox.shell' has no fencing token; denying before any mutation (fail-closed)"
    )
    assert "fencing" not in neutral
    assert "ownership" not in neutral
    assert neutral


def test_bot_failure_text_appends_specific_reason():
    from antigona.channels.telegram.bot import _failure_text_with_reason

    text = _failure_text_with_reason(
        "Задача не выполнена.", "mcp server 'edge-tts' is not registered"
    )
    assert "Задача не выполнена." in text
    assert "not registered" in text


def test_bot_failure_text_hides_internal_wording():
    from antigona.channels.telegram.bot import _failure_text_with_reason

    text = _failure_text_with_reason(
        "Задача не выполнена.",
        "ownership enabled but write surface has no fencing token",
    )
    assert "fencing" not in text
    assert "ownership" not in text


# ── Voice marker survives JSON serialization (generation == delivery) ────────


def test_voice_marker_is_rebuilt_from_escaped_tool_json():
    import json

    from antigona.conversation.dialogue_engine import voice_marker_from_tool_result

    open_bracket = chr(0x27EA)
    close_bracket = chr(0x27EB)
    path = "/var/lib/antigona/.voice_cache/x.ogg"
    payload = json.dumps(
        {
            "success": True,
            "data": {"audio_path": path, "voice_marker": open_bracket + "voice:" + path + close_bracket},
            "artifacts": [{"type": "audio", "path": path}],
        }
    )
    # json.dumps escapes U+27EA/U+27EB, so the raw dump cannot match the
    # channel's delivery regex — the marker must be rebuilt from audio_path.
    assert open_bracket not in payload
    assert voice_marker_from_tool_result(payload) == open_bracket + "voice:" + path + close_bracket


def test_voice_marker_absent_on_tool_error():
    from antigona.conversation.dialogue_engine import voice_marker_from_tool_result

    assert voice_marker_from_tool_result('{"error": "TTS failed"}') == ""


def test_owner_trigger_text_routes_to_the_real_tts_branch():
    """The exact owner trigger text must reach the TTS branch, not a pure
    identity question (which let the model narrate voicing without audio)."""
    from antigona.router.intent_router import IntentRouter

    decision = IntentRouter().route(OWNER_PHANTOM_TEXT)
    assert decision.intent == "task.mcp", (
        f"combined identity+TTS request must route to the execution branch, got "
        f"{decision.intent} (reason={decision.reason_code})"
    )


# ── Router precision: a complaint is not a task ──────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["озвучка не работает", "почему ты не озвучил рассказ", "расскажи про озвучку"],
)
def test_router_does_not_route_tts_complaint_to_task(text):
    from antigona.router.intent_router import IntentRouter

    decision = IntentRouter().route(text)
    assert decision.intent != "task.mcp", (
        f"a meta/negated TTS mention must not become a task flow (got "
        f"{decision.intent}, reason={decision.reason_code})"
    )
