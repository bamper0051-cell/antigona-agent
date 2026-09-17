"""Tests for the shell-command-from-free-text feature (WIP brain/task_backend).

Verifies:
- ``_extract_shell_command`` heuristics (pure ascii, Russian wrapper verbs,
  cyrillic tails rejected);
- ``AntigonaBrain`` routes a shell phrase to ``task.shell``/``ambiguous`` with
  ``tool_name=sandbox.shell`` + ``command`` when the extractor fires;
- the backend contract forwards ``tool_name``/``command`` to the service.
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.core.brain import AntigonaBrain, _extract_shell_command
from antigona.core.task_backend import GatewayTaskBackend


class TestExtractShellCommand:
    """Heuristics: pure ascii, wrapper verbs, cyrillic rejection."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("ls -a", "ls -a"),
            ("pwd", "pwd"),
            ("uptime", "uptime"),
            ("Выполни pwd", "pwd"),
            ("выполните ls -la /tmp", "ls -la /tmp"),
            ("запусти uptime", "uptime"),
            ("прогони pytest -q", "pytest -q"),
            ("покажи /etc/hosts", "/etc/hosts"),
            ("проверь disk usage", "disk usage"),
            ("найди /var/log/nginx", "/var/log/nginx"),
            ("прочитай /etc/os-release", "/etc/os-release"),
            ("Выполни 'echo hi'", "echo hi"),
            ('Запусти "df -h"', "df -h"),
            ("проверь диск", None),  # cyrillic tail → not a shell command
            ("создай файл x.txt", None),  # not a wrapper verb
            ("привет", None),
            ("", None),
            (None, None),
            ("   ", None),
        ],
    )
    def test_extract(self, text: str | None, expected: str | None) -> None:
        assert _extract_shell_command(text) == expected

    def test_extract_keeps_punctuation_trim(self) -> None:
        assert _extract_shell_command("Выполни ls -la;") == "ls -la"
        assert _extract_shell_command("Выполни ls -la.") == "ls -la"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # NL install requests fold to a real pip command.
            ("Install edge-tts", "pip install edge-tts"),
            ("install uv", "pip install uv"),
            ("Установи uv", "pip install uv"),
            ("установи edge-tts", "pip install edge-tts"),
            ("поставь six", "pip install six"),
            ("postav pytest", "pip install pytest"),
            # Already-real commands are left alone (tool name, not bare verb).
            ("apt update", "apt update"),
            ("apt install uv", "apt install uv"),
            ("pip install uv", "pip install uv"),
            ("uv add foo", "uv add foo"),
        ],
    )
    def test_extract_install_nl_translation(self, text: str, expected: str) -> None:
        assert _extract_shell_command(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # A bare URL must NOT become a shell command.
            ("https://example.com", None),
            ("https://habr.com/ru/articles/123/", None),
            ("http://example.org/page?q=1#frag", None),
            # URL inside a real request still routes normally (not bare URL).
            ("Выполни ls -la", "ls -la"),
            ("Install edge-tts", "pip install edge-tts"),
            ("pwd", "pwd"),
        ],
    )
    def test_extract_bare_url_not_shell(self, text: str, expected: str | None) -> None:
        assert _extract_shell_command(text) == expected

    def test_url_helpers(self) -> None:
        from antigona.core.brain import _extract_urls, _is_bare_url

        assert _is_bare_url("https://example.com") is True
        assert _is_bare_url(" https://example.com/ ") is True
        assert _is_bare_url("изучи https://example.com статью") is False
        assert _extract_urls("a https://x.com b http://y.org c https://x.com") == [
            "https://x.com",
            "http://y.org",
        ]
        assert _extract_urls("нет ссылок") == []


class _RecordingBackend:
    """TaskBackend stub that records how submit_task was called."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        owner_id: str | None = None,
        correlation_id: str | None = None,
        tool_name: str | None = None,
        command: tuple[str, ...] = (),
        path: str | None = None,
        content: str | None = None,
        mcp_server: str = "",
        mcp_tool: str = "",
        mcp_arguments: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "message": message,
                "tool_name": tool_name,
                "command": command,
                "owner_id": owner_id,
                "correlation_id": correlation_id,
            }
        )
        return {"flow_id": "flow-x", "id": "flow-x", "status": "QUEUED", "requires_approval": False}

    async def cancel_flow(self, flow_id: str) -> Any:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED"})()


@pytest.mark.asyncio
async def test_brain_shell_phrase_routes_with_tool_and_command() -> None:
    """A shell phrase ('Выполни pwd') becomes sandbox.shell + command."""
    backend = _RecordingBackend()
    brain = AntigonaBrain(db_path=":memory:", task_backend=backend)
    await brain.connect()
    try:
        resp = await brain.process(
            text="Выполни pwd",
            user_id="owner-1",
            channel="cli",
            session_id="owner-1:s",
            context={"owner_id": "owner-1", "correlation_id": "corr-1"},
        )
    finally:
        await brain.close()

    assert resp.response_type == "task_accepted"
    assert backend.calls, "submit_task must be called"
    call = backend.calls[-1]
    assert call["tool_name"] == "sandbox.shell"
    assert call["command"] == ("pwd",)
    assert call["owner_id"] == "owner-1"
    assert call["correlation_id"] == "corr-1"


@pytest.mark.asyncio
async def test_brain_plain_text_keeps_default_tool() -> None:
    """Free text without a shell phrase keeps the default tool (None override)."""
    backend = _RecordingBackend()
    brain = AntigonaBrain(db_path=":memory:", task_backend=backend)
    await brain.connect()
    try:
        resp = await brain.process(
            text="привет, как дела",
            user_id="owner-1",
            channel="cli",
            session_id="owner-1:s",
            context={"owner_id": "owner-1", "correlation_id": "corr-2"},
        )
    finally:
        await brain.close()

    if backend.calls:
        call = backend.calls[-1]
        # A pure conversation must not be sent as sandbox.shell.
        assert call["tool_name"] != "sandbox.shell" or resp.response_type != "task_accepted"


@pytest.mark.asyncio
async def test_task_backend_forwards_tool_and_command() -> None:
    """GatewayTaskBackend passes tool_name/command through to the service."""
    submitted: dict[str, Any] = {}

    class _Svc:
        async def submit_async(self, **kwargs: Any) -> dict[str, Any]:
            submitted.update(kwargs)
            return {"flow_id": "flow-y", "id": "flow-y", "status": "QUEUED"}

    backend = GatewayTaskBackend(database=None)  # type: ignore[arg-type]
    backend._service = _Svc()  # type: ignore[attr-defined]
    await backend.submit_task(
        message="Выполни pwd",
        owner_id="owner-1",
        correlation_id="corr-3",
        tool_name="sandbox.shell",
        command=("pwd",),
    )
    assert submitted["tool_name"] == "sandbox.shell"
    assert submitted["command"] == ("pwd",)
    assert submitted["owner_id"] == "owner-1"


class TestShellRiskPredictionAndNotification:
    """A natural-language shell request must actually run end to end, and the
    user must be told upfront (not left silently guessing) whenever it will
    actually need a manual /approve.

    Regression coverage for the "Antigona still can't run commands" bug: a
    benign 'ls'/'uptime'/'echo'-style request used to be silently parked in
    WAITING_APPROVAL behind an unconditional "Задача принята и выполняется."
    reply, with requires_approval always false, and a substring bug in
    evaluate_risk() classified any command whose text merely *contained*
    "rm" (e.g. "echo hermes...", "confirm", "term...") as HIGH risk.
    """

    @pytest.mark.asyncio
    async def test_benign_command_does_not_predict_approval(self) -> None:
        """'ls'/'echo'/'uptime'-style commands run without a manual /approve."""
        backend = _RecordingBackend()
        brain = AntigonaBrain(db_path=":memory:", task_backend=backend)
        await brain.connect()
        try:
            resp = await brain.process(
                text="Выполни echo hello_from_hermes_test",
                user_id="owner-1",
                channel="cli",
                session_id="owner-1:s",
                context={"owner_id": "owner-1", "correlation_id": "corr-low"},
            )
        finally:
            await brain.close()

        assert resp.response_type == "task_accepted"
        assert resp.requires_approval is False
        assert resp.text == "Задача принята и выполняется."
        # The literal substring "rm" inside "hermes" must not escalate risk.
        from antigona.worker.hitl import RiskLevel, evaluate_risk

        risk, _ = evaluate_risk("sandbox.shell", {"command": ["echo hello_from_hermes_test"]})
        assert risk == RiskLevel.LOW

    @pytest.mark.asyncio
    async def test_risky_command_predicts_approval_and_says_so(self) -> None:
        """A destructive command tells the user upfront it needs /approve."""
        backend = _RecordingBackend()
        brain = AntigonaBrain(db_path=":memory:", task_backend=backend)
        await brain.connect()
        try:
            resp = await brain.process(
                text="Выполни rm -rf /tmp/whatever",
                user_id="owner-1",
                channel="cli",
                session_id="owner-1:s",
                context={"owner_id": "owner-1", "correlation_id": "corr-high"},
            )
        finally:
            await brain.close()

        assert resp.response_type == "task_accepted"
        assert resp.requires_approval is True
        assert "/approve" in resp.text
