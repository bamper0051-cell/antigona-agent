"""DEFECT-P0-040 — explicit CLI file content lost before canonical execution.

Observed: `antigona run "создай <path> и запиши туда <CONTENT>"` created the
file but with content="" — the literal content the user supplied in the
"запиши туда / сюда / в него" continuation never reached the canonical
Gateway request, so the write tool wrote an empty file.

RCA boundary: ``antigona.task_goal._extract_content`` (via ``parse_goal``) had
no pattern for the "write-into-it" continuation. The CLI (``antigona.cli.run``)
and the Gateway client faithfully forward whatever ``parse_goal`` returns, so
``content=""`` propagated all the way to ``workspace.write_text``.

These tests pin:
  RED-1  parse_goal preserves the literal content for the continuation form.
  RED-2  the CLI forwards that content into ``GatewayClient.create_flow``.
  RED-3  the exact requested string (text and JSON) survives to the request.
  RED-4  an explicitly-empty-file request still yields content="" (no
         hallucinated / reconstructed content) — empty is valid ONLY here.
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.task_goal import parse_goal

# ── RED-1 / RED-3 : parser boundary ─────────────────────────────────────────

CONTINUATION_CASES = [
    ("создай exam/p0/cli_test.txt и запиши туда CLI-CONTENT",
     "exam/p0/cli_test.txt", "CLI-CONTENT"),
    ("создай файл exam/p0/cli_content_probe.txt и запиши туда CLI-CONTENT-PROBE",
     "exam/p0/cli_content_probe.txt", "CLI-CONTENT-PROBE"),
    ("создай файл a.txt и запиши туда HELLO", "a.txt", "HELLO"),
    ("создай a.txt и запиши в него HELLO", "a.txt", "HELLO"),
    ("создай a.txt, запиши туда HELLO", "a.txt", "HELLO"),
    ("создай файл notes.txt и запиши сюда WORLD", "notes.txt", "WORLD"),
]


@pytest.mark.parametrize("goal,expected_path,expected_content", CONTINUATION_CASES)
def test_red1_parse_goal_preserves_continuation_content(
    goal: str, expected_path: str, expected_content: str
) -> None:
    plan = parse_goal(goal)
    assert plan.intent == "file_write", plan.intent
    assert plan.path == expected_path, f"path={plan.path!r}"
    assert plan.content == expected_content, f"content={plan.content!r}"


def test_red3_json_content_survives_continuation() -> None:
    plan = parse_goal('создай exam/p0/cli.json и запиши туда {"source":"cli"}')
    assert plan.intent == "file_write"
    assert plan.path == "exam/p0/cli.json"
    assert plan.content == '{"source":"cli"}'


# ── RED-4 : explicit empty file stays empty (no reconstruction) ─────────────

def test_red4_explicit_empty_file_has_empty_content() -> None:
    plan = parse_goal("создай пустой файл empty.txt")
    assert plan.intent == "file_write"
    assert plan.path == "empty.txt"
    assert plan.content == ""


# ── control : existing supported forms must keep working ───────────────────

@pytest.mark.parametrize("goal,expected", [
    ("создай файл a.txt с текстом HELLO", "HELLO"),
    ("создай файл b.txt с содержимым WORLD", "WORLD"),
    ('создай JSON exam/p0/cli.json: {"source":"cli"}', '{"source":"cli"}'),
])
def test_control_existing_forms_unchanged(goal: str, expected: str) -> None:
    assert parse_goal(goal).content == expected


# ── RED-2 : CLI forwards parsed content into the canonical request ─────────

class _FakeGatewayClient:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def create_flow(self, **kwargs: Any) -> dict[str, Any]:
        type(self).last_kwargs = dict(kwargs)
        return {"id": "flow-test-1", "correlation_id": "cid-1", "status": "RECEIVED"}

    async def send_dialogue_turn(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"reply": ""}

    async def get_result(self, *_a: Any, **_kw: Any) -> Any:  # pragma: no cover
        raise AssertionError("get_result must not be called for a write flow")


def test_red2_cli_run_forwards_continuation_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import antigona.cli as cli

    monkeypatch.setenv("ANTIGONA_GATEWAY_URL", "http://gw.test")
    monkeypatch.setenv("ANTIGONA_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("ANTIGONA_STATE_FILE", str(tmp_path / "cli_state.json"))
    monkeypatch.setattr(cli, "GatewayClient", _FakeGatewayClient)
    _FakeGatewayClient.last_kwargs = {}

    cli.run(
        goal="создай exam/p0/cli_content_probe.txt и запиши туда CLI-CONTENT-PROBE",
        attach_stream=False,
    )

    kw = _FakeGatewayClient.last_kwargs
    assert kw, "GatewayClient.create_flow was never called"
    assert kw["path"] == "exam/p0/cli_content_probe.txt", kw
    assert kw["content"] == "CLI-CONTENT-PROBE", kw
    assert kw["tool_name"] == "workspace.write_text", kw


def test_red2_cli_run_empty_file_forwards_empty_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import antigona.cli as cli

    monkeypatch.setenv("ANTIGONA_GATEWAY_URL", "http://gw.test")
    monkeypatch.setenv("ANTIGONA_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("ANTIGONA_STATE_FILE", str(tmp_path / "cli_state.json"))
    monkeypatch.setattr(cli, "GatewayClient", _FakeGatewayClient)
    _FakeGatewayClient.last_kwargs = {}

    cli.run(goal="создай пустой файл empty.txt", attach_stream=False)

    kw = _FakeGatewayClient.last_kwargs
    assert kw, "GatewayClient.create_flow was never called"
    assert kw["path"] == "empty.txt", kw
    assert kw["content"] == "", kw
