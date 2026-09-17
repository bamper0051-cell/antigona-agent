"""Regression guard: the new slash commands must route to their own intents
(not fall through to command.help) and the brain must dispatch them.
"""
import pytest

from antigona.router.intent_router import IntentRouter


@pytest.fixture()
def router():
    return IntentRouter()


@pytest.mark.parametrize("cmd,intent", [
    ("/health", "command.health"),
    ("/commands", "command.commands"),
    ("/session", "command.session"),
    ("/history", "command.history"),
    ("/memory", "command.memory"),
    ("/sysinfo", "command.sysinfo"),
    ("/web python", "command.web"),
    ("/ollama status", "command.ollama"),
    ("/install pip six", "command.install"),
    ("/list", "command.list"),
    ("/tasks", "command.list"),
    ("/get abc", "command.get"),
    ("/steer abc fix", "command.steer"),
    ("/approvals", "command.approvals"),
    ("/approve 1", "command.approve"),
    ("/deny 1", "command.deny"),
])
def test_new_commands_route_to_own_intent(router, cmd, intent):
    d = router.route(cmd, context={})
    assert d.intent == intent, f"{cmd} routed to {d.intent}, expected {intent}"


def test_new_commands_not_help(router):
    for cmd in ["/health", "/memory", "/web", "/install", "/sysinfo", "/history", "/session"]:
        d = router.route(cmd, context={})
        assert d.intent != "command.help", f"{cmd} fell through to help"

@pytest.mark.parametrize("cmd,intent", [
    ("/image a fox", "command.image"),
    ("/img a fox", "command.image"),
])
def test_image_command_routes(router, cmd, intent):
    assert router.route(cmd, context={}).intent == intent

@pytest.mark.parametrize("text,is_img,prompt", [
    ("нарисуй кота в шляпе", True, "кота в шляпе"),
    ("сгенерируй картинку красной лисы", True, "картинку красной лисы"),
    ("draw a cat", True, "a cat"),
    ("как дела?", False, None),
    ("расскажи про архитектуру", False, None),
])
def test_image_request_detection(text, is_img, prompt):
    from antigona.core.brain import AntigonaBrain
    assert AntigonaBrain._is_image_request(text) is is_img
    if is_img:
        assert AntigonaBrain._image_prompt_from(text) == prompt

@pytest.mark.parametrize("cmd,intent", [
    ("/tts привет мир", "command.tts"),
    ("/voice привет", "command.tts"),
])
def test_tts_command_routes(router, cmd, intent):
    assert router.route(cmd, context={}).intent == intent
