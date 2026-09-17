"""Tests for the static CLI status panel (banner + panel, no TUI animations).

Ensures the panel:
- renders banner + bordered panel fed by REAL state (flows/approvals/status);
- has a stable, fixed height (critical for in-place redraws);
- stays within a bounded width (no line wrapping inside the panel box);
- works with and without colour;
- exposes a full HELP_TEXT covering every supported command.
"""

from __future__ import annotations

import sys
from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from antigona.cli_ui.commands import (
    HELP_TEXT,
    CommandKind,
    parse_command,
)
from antigona.cli_ui.models import ChatUIState
from antigona.cli_ui.panel import (
    marker,
    panel_height,
    render_panel,
)


def _state(
    status: str = "idle",
    flows: list[dict[str, Any]] | None = None,
    approvals: list[dict[str, Any]] | None = None,
    last_event: str = "",
    connection: str = "connected",
    events: list[dict[str, Any]] | None = None,
    session_id: str = "cli-session",
) -> ChatUIState:
    return ChatUIState(
        gateway_url="http://127.0.0.1:8090",
        session_id=session_id,
        current_status=status,
        active_flows=flows or [],
        pending_approvals=approvals or [],
        last_event=last_event,
        connection=connection,
        events=events or [],
    )


def _render(state: ChatUIState, width: int = 100, no_color: bool = False) -> str:
    buf = StringIO()
    console = Console(file=buf, width=width, force_terminal=False, no_color=no_color)
    render_panel(console, state)
    return buf.getvalue()


class TestPanelRender:
    """Static panel renders banner + data-driven body."""

    @pytest.mark.skipif(sys.platform == "win32", reason='box-drawing panel requires a Windows console (NoConsoleScreenBufferError) (Wave 4)')
    def test_panel_has_banner_and_borders(self) -> None:
        out = _render(_state())
        normalized = " ".join(out.split())
        assert "Control Center" in normalized
        assert "ПУЛЬТ" in out
        assert "╭─" in out and "╰─" in out

    def test_panel_compact_banner_has_antigona_word(self) -> None:
        out = _render(_state(), width=44)
        assert "ANTIGONA" in out

    def test_panel_shows_gateway_and_session(self) -> None:
        out = _render(_state())
        assert "connected" in out
        assert "cli-session" in out

    def test_panel_shows_status_and_counts(self) -> None:
        out = _render(_state(status="planning", flows=[{"flow_id": "f1", "status": "PLANNING"}]))
        assert "планирование" in out
        assert "флоу: 1" in out

    def test_panel_shows_waiting_approval(self) -> None:
        out = _render(
            _state(
                status="waiting_approval",
                approvals=[{"approval_id": "app-1", "reason": "git push"}],
            )
        )
        assert "ждёт одобрения" in out
        assert "app-1" in out

    def test_panel_shows_last_event(self) -> None:
        out = _render(_state(last_event="task accepted"))
        assert "task accepted" in out

    def test_panel_shows_event_feed(self) -> None:
        out = _render(
            _state(
                status="verifying",
                events=[{"event": "plan", "detail": "план построен"}],
            )
        )
        assert "план построен" in out

    def test_panel_shows_reconnecting(self) -> None:
        out = _render(_state(connection="reconnecting", status="reconnecting"))
        assert "переподключение" in out

    def test_panel_height_is_stable_all_widths(self) -> None:
        states = [
            ("idle", [], []),
            ("planning", [{"flow_id": "f1", "status": "PLANNING"}], []),
            (
                "waiting_approval",
                [{"flow_id": "f1", "status": "WAITING_APPROVAL"}],
                [{"approval_id": "a1", "reason": "rm -rf /tmp/x"}],
            ),
            ("tool_executing", [{"flow_id": "f1", "status": "EXECUTING"}], []),
            ("verifying", [{"flow_id": "f1", "status": "VERIFYING"}], []),
            ("done", [], []),
            ("failed", [], []),
            ("cancelled", [], []),
            ("reconnecting", [], []),
        ]
        for width in (40, 45, 50, 60, 80, 100):
            for status, flows, apps in states:
                state = _state(
                    status=status,
                    flows=flows,
                    approvals=apps,
                    connection="reconnecting" if status == "reconnecting" else "connected",
                    events=[
                        {"event": "plan", "detail": "очень длинный текст для проверки"},
                        {"event": "tool", "detail": "file.write /path/to/long/file"},
                    ],
                )
                out = _render(state, width=width)
                lines = out.splitlines()
                expected = panel_height(state, min(width, 82) - 4)
                assert len(lines) == expected, (
                    f"height unstable w={width} status={status}: "
                    f"{len(lines)} != {expected}"
                )

    def test_panel_lines_stay_within_bounded_width(self) -> None:
        out = _render(
            _state(
                status="waiting_approval",
                flows=[{"flow_id": "flow-a1b2c3", "status": "WAITING_APPROVAL", "title": "x" * 120}],
                approvals=[{"approval_id": "app-99", "reason": "y" * 200}],
                last_event="z" * 300,
            ),
            width=100,
        )
        max_len = max(len(line) for line in out.splitlines())
        # Panel body width + borders; every body line must fit inside the box.
        assert max_len <= 84, f"panel line overflow: {max_len}"

    def test_panel_renders_without_color(self) -> None:
        out = _render(_state(), width=100, no_color=True)
        assert "127.0.0.1:8090" in out
        assert "cli-session" in out
        assert "\x1b[" not in out
        # ASCII fallback markers instead of emoji in plain mode.
        assert "[OK]" in out
        assert "ANTIGONA" in out

    def test_narrow_terminal_40_columns(self) -> None:
        out = _render(
            _state(
                status="planning",
                session_id="84c1a2f3deadbeef",
                flows=[{"flow_id": "b7fc8f92", "status": "PLANNING", "title": "e2e"}],
            ),
            width=40,
        )
        max_len = max(len(line) for line in out.splitlines())
        assert max_len <= 44, f"narrow panel overflow: {max_len}"

    def test_wide_terminal_shows_more(self) -> None:
        wide = _render(_state(status="planning"), width=100)
        narrow = _render(_state(status="planning"), width=40)
        assert "флоу: 0" in wide
        assert "f:0" in narrow


class TestMarkers:
    """Status markers: emoji by default, ASCII fallback."""

    def test_marker_emoji(self) -> None:
        assert marker("waiting_approval", emoji=True) == "🔐"
        assert marker("done", emoji=True) == "✅"
        assert marker("failed", emoji=True) == "❌"
        assert marker("reconnecting", emoji=True) == "🔄"

    def test_marker_ascii_fallback(self) -> None:
        assert marker("waiting_approval", emoji=False) == "[WAIT]"
        assert marker("done", emoji=False) == "[OK]"
        assert marker("failed", emoji=False) == "[ERR]"
        assert marker("tool_executing", emoji=False) == "[RUN]"

    def test_unknown_status_falls_back(self) -> None:
        assert marker("nonexistent", emoji=True) == "⚪"
        assert marker("nonexistent", emoji=False) == "[--]"


class TestHelpText:
    """HELP_TEXT lists every supported command with a hint."""

    ALL_COMMANDS = (
        "/help",
        "/exit",
        "/quit",
        "/status",
        "/list",
        "/tasks",
        "/get",
        "/cancel",
        "/steer",
        "/approvals",
        "/approve",
        "/deny",
        "/health",
        "/commands",
        "/session",
        "/history",
        "/memory",
    )

    def test_help_lists_every_command(self) -> None:
        for cmd in self.ALL_COMMANDS:
            assert cmd in HELP_TEXT, f"HELP_TEXT missing {cmd}"

    def test_help_has_usage_hints(self) -> None:
        for token in ("flow_id", "approval_id", "session_id", "текст"):
            assert token in HELP_TEXT, f"HELP_TEXT missing hint token {token}"

    def test_new_commands_parse(self) -> None:
        cases = {
            "/tasks": CommandKind.TASKS,
            "/health": CommandKind.HEALTH,
            "/commands": CommandKind.COMMANDS,
            "/session abc": CommandKind.SESSION,
            "/history abc": CommandKind.HISTORY,
            "/memory": CommandKind.MEMORY,
        }
        for raw, kind in cases.items():
            parsed = parse_command(raw)
            assert parsed.kind == kind, f"{raw} → {parsed.kind} (want {kind})"

    def test_help_command_still_works(self) -> None:
        parsed = parse_command("/help")
        assert parsed.kind == CommandKind.HELP


class _MockGateway:
    """Minimal structural GatewayClient for dispatch tests of new commands."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def health(self) -> dict[str, Any]:
        self.calls.append("health")
        return {"status": "ok"}

    async def list_commands(self, channel: str = "all") -> list[dict[str, object]]:
        self.calls.append("commands")
        return [{"name": "help", "description": "help"}]

    async def session_info(self, session_id: str) -> dict[str, Any]:
        self.calls.append(f"session:{session_id}")
        return {"session_id": session_id}

    async def session_history(self, session_id: str, limit: int = 100) -> dict[str, Any]:
        self.calls.append(f"history:{session_id}")
        return {"session_id": session_id, "items": []}

    async def memory_list(
        self, *, kind: str | None = None, query: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        self.calls.append("memory")
        return {"items": []}

    async def list_flows(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append("flows")
        return []


class TestNewCommandsDispatch:
    """New gateway commands dispatch through the client."""

    async def test_health_dispatches(self) -> None:
        from antigona.cli_ui.commands import CommandDisposition, dispatch_command

        mock = _MockGateway()
        cmd = parse_command("/health")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert "health" in mock.calls

    async def test_commands_dispatches(self) -> None:
        from antigona.cli_ui.commands import CommandDisposition, dispatch_command

        mock = _MockGateway()
        cmd = parse_command("/commands")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert "commands" in mock.calls

    async def test_session_dispatches(self) -> None:
        from antigona.cli_ui.commands import CommandDisposition, dispatch_command

        mock = _MockGateway()
        cmd = parse_command("/session abc-123")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert "session:abc-123" in mock.calls

    async def test_history_dispatches(self) -> None:
        from antigona.cli_ui.commands import CommandDisposition, dispatch_command

        mock = _MockGateway()
        cmd = parse_command("/history abc-123")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert "history:abc-123" in mock.calls

    async def test_memory_dispatches(self) -> None:
        from antigona.cli_ui.commands import CommandDisposition, dispatch_command

        mock = _MockGateway()
        cmd = parse_command("/memory")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert "memory" in mock.calls

    async def test_tasks_dispatches_to_flows(self) -> None:
        from antigona.cli_ui.commands import CommandDisposition, dispatch_command

        mock = _MockGateway()
        cmd = parse_command("/tasks")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert "flows" in mock.calls

    async def test_session_missing_id_fails_closed(self) -> None:
        from antigona.cli_ui.commands import CommandDisposition, dispatch_command

        mock = _MockGateway()
        cmd = parse_command("/session")
        assert cmd.kind == CommandKind.MALFORMED
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.FAIL_CLOSED
        assert mock.calls == []


class _RecordingRenderer:
    """Renderer stub that records panel redraws for monitor tests."""

    def __init__(self) -> None:
        self.panels: list[tuple[str, str, list[dict[str, Any]]]] = []

    def render_panel(self, state: ChatUIState) -> None:
        self.panels.append(("init", state.current_status, list(state.events)))

    def update_panel(self, state: ChatUIState) -> None:
        self.panels.append(("update", state.current_status, list(state.events)))

    def render_message(self, role: Any, content: str, width: int | None = None) -> None:
        pass

    def render_state(self, state: ChatUIState) -> None:
        pass

    def render_outcome(self, outcome: Any) -> None:
        pass

    def release(self) -> None:
        pass


class _EventsGateway:
    """Gateway stub that feeds canned transition events once, then goes down."""

    def __init__(self, events: list[dict[str, Any]], then_fail: bool = False) -> None:
        self._events = list(events)
        self._then_fail = then_fail
        self.calls = 0

    async def get_events(self, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        self.calls += 1
        if self._then_fail and self.calls > 1:
            raise RuntimeError("gateway down")
        # Return only events newer than the caller's cursor (like GET /events).
        new_events = [ev for ev in self._events if int(ev.get("seq", 0)) > after_seq]
        return new_events

    async def list_flows(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def list_approvals(self, *args: Any, **kwargs: Any) -> Any:
        return type("A", (), {"items": []})()

    async def close(self) -> None:
        pass


class TestMonitor:
    """Live event monitor feeds the panel with real transitions."""

    async def test_monitor_maps_states_and_feed(self) -> None:
        from antigona.cli_ui.chat import ChatController

        gw = _EventsGateway(
            [
                {"seq": 1, "to_state": "PLANNING", "flow_id": "flow1"},
                {"seq": 2, "to_state": "TOOL_EXECUTING", "flow_id": "flow1"},
            ]
        )
        renderer = _RecordingRenderer()
        ctrl = ChatController(gateway=gw, renderer=renderer, enable_animations=False)
        await ctrl.start_monitor()
        # Let the loop poll a couple of times.
        for _ in range(40):
            if len(renderer.panels) >= 2:
                break
            import asyncio

            await asyncio.sleep(0.05)
        await ctrl.stop_monitor()

        last_status, last_events = renderer.panels[-1][1], renderer.panels[-1][2]
        assert last_status == "tool_executing"
        assert [e["event"] for e in last_events] == ["план", "инструмент"]
        assert ctrl.state.connection == "connected"

    async def test_monitor_detects_gateway_outage(self) -> None:
        from antigona.cli_ui.chat import ChatController

        gw = _EventsGateway([{"seq": 1, "to_state": "DONE", "flow_id": "flow1"}], then_fail=True)
        renderer = _RecordingRenderer()
        ctrl = ChatController(gateway=gw, renderer=renderer, enable_animations=False)
        await ctrl.start_monitor()
        for _ in range(60):
            if ctrl.state.connection == "reconnecting":
                break
            import asyncio

            await asyncio.sleep(0.05)
        await ctrl.stop_monitor()
        assert ctrl.state.connection == "reconnecting"
        assert ctrl.state.current_status == "reconnecting"

    async def test_monitor_ignores_unknown_payloads(self) -> None:
        from antigona.cli_ui.chat import ChatController

        gw = _EventsGateway(
            [
                {"seq": 1, "to_state": "DONE", "flow_id": "flow1", "secret_key": "should-not-leak"},
                "not-a-dict",
                None,
            ]
        )
        renderer = _RecordingRenderer()
        ctrl = ChatController(gateway=gw, renderer=renderer, enable_animations=False)
        await ctrl.start_monitor()
        for _ in range(40):
            if ctrl.state.current_status == "done":
                break
            import asyncio

            await asyncio.sleep(0.05)
        await ctrl.stop_monitor()

        # Only safe, structured fields are stored — never raw payloads.
        assert "secret_key" not in str(ctrl.state.events)
        assert "should-not-leak" not in str(ctrl.state.events)

    async def test_reconnect_clears_stuck_reconnecting_status(self) -> None:
        """After a Gateway restart the monitor clears 'reconnecting'."""
        from antigona.cli_ui.chat import ChatController

        events = [{"seq": 1, "to_state": "PLANNING", "flow_id": "flow1"}]

        class _FailingThenOkGateway(_EventsGateway):
            def __init__(self) -> None:
                super().__init__(events)
                self.failures_left = 3
                self.flow_status = "WAITING_APPROVAL"

            async def get_events(self, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
                if self.failures_left > 0:
                    self.failures_left -= 1
                    raise RuntimeError("gateway down")
                return await super().get_events(after_seq=after_seq, limit=limit)

            async def list_flows(self, *args: Any, **kwargs: Any) -> list[Any]:
                # A real active flow with a real status after the restart.
                return [
                    type(
                        "FV",
                        (),
                        {"status": self.flow_status, "flow_id": "flow1", "title": "t"},
                    )()
                ]

        gw = _FailingThenOkGateway()
        renderer = _RecordingRenderer()
        ctrl = ChatController(gateway=gw, renderer=renderer, enable_animations=False)
        await ctrl.start_monitor()
        # Wait until the outage is seen and then recovered.
        saw_reconnecting = False
        for _ in range(120):
            import asyncio

            if ctrl.state.connection == "reconnecting":
                saw_reconnecting = True
            if saw_reconnecting and ctrl.state.connection == "connected":
                break
            await asyncio.sleep(0.05)
        await ctrl.stop_monitor()

        assert saw_reconnecting, "outage must be observed"
        assert ctrl.state.connection == "connected", "connection must be restored"
        # The panel status must leave "reconnecting" and reflect a real
        # transition (the monitor consumes the buffered PLANNING event) or the
        # rehydrated flow status — never stay stuck on the outage marker.
        assert ctrl.state.current_status != "reconnecting"
        assert len(ctrl.state.active_flows) == 1


class TestSlashMenu:
    """Bare "/" shows the full command menu; autocomplete filters."""

    async def test_bare_slash_shows_menu(self) -> None:
        from antigona.cli_ui.chat import ChatController

        renderer = _RecordingRenderer()
        ctrl = ChatController(gateway=_MockGateway(), renderer=renderer, enable_animations=False)
        await ctrl.handle_input("/")
        last = renderer.panels  # noqa: F841
        # Menu content is appended as an INFO message.
        assert any(
            getattr(m, "role", None) and str(getattr(m, "role", "")).endswith("info")
            for m in ctrl.state.messages
        )
        menu_text = "\n".join(
            str(m.content) for m in ctrl.state.messages if "Команды" in str(m.content)
        )
        assert "/help" in menu_text and "/approve" in menu_text and "/memory" in menu_text

    def test_autocomplete_filters(self) -> None:
        from prompt_toolkit.document import Document

        from antigona.cli_ui.prompts import DEFAULT_SLASH_COMMANDS, SlashCommandCompleter

        completer = SlashCommandCompleter(DEFAULT_SLASH_COMMANDS)
        results = list(completer.get_completions(Document(text="/ap")))
        names = [c.text for c in results]
        assert "/approve" in names
        assert "/approvals" in names
        assert "/help" not in names

    def test_autocomplete_includes_usage_hints(self) -> None:
        from prompt_toolkit.document import Document

        from antigona.cli_ui.prompts import DEFAULT_SLASH_COMMANDS, SlashCommandCompleter

        completer = SlashCommandCompleter(DEFAULT_SLASH_COMMANDS)
        results = list(completer.get_completions(Document(text="/app")))
        for c in results:
            if c.text == "/approve":
                assert "<flow_id>" in str(c.display_meta)

    def test_no_duplicate_commands_in_catalog(self) -> None:
        from antigona.cli_ui.prompts import DEFAULT_SLASH_COMMANDS

        names = [c.name for c in DEFAULT_SLASH_COMMANDS]
        assert len(names) == len(set(names)), f"duplicates: {names}"


class TestPhaseText:
    """Animation phase labels are calm, mapped per status."""

    def test_phase_text_mapped(self) -> None:
        from antigona.cli_ui.chat import _phase_text

        assert "планирование" in _phase_text("planning")
        assert "инструмент" in _phase_text("tool_executing")
        assert "Verifier" in _phase_text("verifying")
        assert "подтверждение" in _phase_text("waiting_approval")
        assert "готово" in _phase_text("done")

    def test_phase_text_unknown_falls_back(self) -> None:
        from antigona.cli_ui.chat import _phase_text

        assert "weird" in _phase_text("weird")
