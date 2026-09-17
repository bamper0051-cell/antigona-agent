"""DEFECT-P0-041 — English file-write routing and content extraction remediation.

Observed: English commands such as `write hello.txt with content HELLO` or
`create exam/p0/a.txt with content SOME TEXT` are incorrectly parsed as:
- intent = "dialog"
- path = ""
- content = ""

Expected:
- intent = "file_write"
- path extracted correctly
- content extracted accurately (byte-for-byte exact)
- intentional empty file requests (e.g. `write an empty file empty.txt`) yield content=""
- normal English dialogue (e.g. `write a short poem`) remains dialogue.
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.task_goal import parse_goal

# ── RED : English file-write parsing ────────────────────────────────────────

@pytest.mark.parametrize(
    "goal,expected_path,expected_content",
    [
        ("write hello.txt with content HELLO", "hello.txt", "HELLO"),
        ("write exam/p0/a.txt with content SOME TEXT", "exam/p0/a.txt", "SOME TEXT"),
        ("write exam/p0/a.txt with text SOME TEXT", "exam/p0/a.txt", "SOME TEXT"),
        ("create exam/p0/a.txt with content SOME TEXT", "exam/p0/a.txt", "SOME TEXT"),
        ("write exam/p0/p041_probe.txt with content P041-CONTENT", "exam/p0/p041_probe.txt", "P041-CONTENT"),
        ("create file exam/p0/notes.txt with content NOTE_DATA", "exam/p0/notes.txt", "NOTE_DATA"),
        ("write file data.json with content {\"status\":\"ok\"}", "data.json", "{\"status\":\"ok\"}"),
    ],
)
def test_red_english_file_write_routing_and_content(
    goal: str, expected_path: str, expected_content: str
) -> None:
    plan = parse_goal(goal)
    assert plan.intent == "file_write", f"Expected file_write but got {plan.intent!r} for {goal!r}"
    assert plan.path == expected_path, f"Expected path {expected_path!r} but got {plan.path!r}"
    assert plan.content == expected_content, f"Expected content {expected_content!r} but got {plan.content!r}"


# ── RED : Intentional empty English file requests ───────────────────────────

@pytest.mark.parametrize(
    "goal,expected_path",
    [
        ("write an empty file exam/p0/empty.txt", "exam/p0/empty.txt"),
        ("create an empty file empty.txt", "empty.txt"),
        ("write empty file empty.txt", "empty.txt"),
    ],
)
def test_red_english_intentional_empty_file(goal: str, expected_path: str) -> None:
    plan = parse_goal(goal)
    assert plan.intent == "file_write", f"Expected file_write but got {plan.intent!r} for {goal!r}"
    assert plan.path == expected_path, f"Expected path {expected_path!r} but got {plan.path!r}"
    assert plan.content == "", f"Expected empty content but got {plan.content!r}"


# ── RED : English Dialogue vs File-write Disambiguation ───────────────────

@pytest.mark.parametrize(
    "goal",
    [
        "write a short poem about stars",
        "write a story about a dragon",
        "write a python function to calculate fibonacci numbers",
        "tell me a joke",
        "what is the capital of France?",
    ],
)
def test_english_dialogue_stays_dialogue(goal: str) -> None:
    plan = parse_goal(goal)
    assert plan.intent == "dialog", f"Expected dialog but got {plan.intent!r} for {goal!r}"
    assert plan.path == ""
    assert plan.content == ""


# ── RED : CLI forwarding to GatewayClient ────────────────────────────────────

class _FakeGatewayClient:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def create_flow(self, **kwargs: Any) -> dict[str, Any]:
        type(self).last_kwargs = dict(kwargs)
        return {"id": "flow-p041-test", "correlation_id": "cid-p041", "status": "RECEIVED"}

    async def send_dialogue_turn(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"reply": "dialogue reply"}

    async def get_result(self, *_a: Any, **_kw: Any) -> Any:
        raise AssertionError("get_result must not be called for a write flow")


def test_cli_run_forwards_english_file_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import antigona.cli as cli

    monkeypatch.setenv("ANTIGONA_GATEWAY_URL", "http://gw.test")
    monkeypatch.setenv("ANTIGONA_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("ANTIGONA_STATE_FILE", str(tmp_path / "cli_state.json"))
    monkeypatch.setattr(cli, "GatewayClient", _FakeGatewayClient)
    _FakeGatewayClient.last_kwargs = {}

    cli.run(
        goal="write exam/p0/p041_probe.txt with content P041-CONTENT",
        attach_stream=False,
    )

    kw = _FakeGatewayClient.last_kwargs
    assert kw, "GatewayClient.create_flow was never called"
    assert kw["path"] == "exam/p0/p041_probe.txt", kw
    assert kw["content"] == "P041-CONTENT", kw
    assert kw["tool_name"] == "workspace.write_text", kw
