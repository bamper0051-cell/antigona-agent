"""Tests for the unified Antigona Console (tui_console)."""

from __future__ import annotations

import pytest
from textual.widgets import ContentSwitcher, Log, Tabs

from antigona.tui_console import AntigonaConsole, _clip, _event_row, status_color


@pytest.mark.asyncio
async def test_console_mounts_four_tabs() -> None:
    app = AntigonaConsole(gateway_url="http://127.0.0.1:9", token="x")  # unreachable
    async with app.run_test(size=(120, 40)) as _pilot:
        tabs = app.query_one("#main_tabs", Tabs)
        assert len(tabs._tabs) == 4
        switcher = app.query_one("#panes", ContentSwitcher)
        assert "pane-chat" in [w.id for w in switcher.children]
        assert "pane-activity" in [w.id for w in switcher.children]
        assert "pane-flows" in [w.id for w in switcher.children]
        assert "pane-approvals" in [w.id for w in switcher.children]
    app.exit()


@pytest.mark.asyncio
async def test_console_status_bar_exists() -> None:
    app = AntigonaConsole(gateway_url="http://127.0.0.1:9", token="x")
    async with app.run_test(size=(120, 40)) as _pilot:
        bar = app.query_one("#status_bar")
        # unreachable gateway → connection check fails closed
        assert getattr(bar, "connected", False) in (False, None)
        assert app.role == "orchestrator"
    app.exit()


@pytest.mark.asyncio
async def test_toggle_role_flips() -> None:
    app = AntigonaConsole(gateway_url="http://127.0.0.1:9", token="x")
    async with app.run_test(size=(120, 40)) as _pilot:
        app.action_toggle_role()
        assert app.role == "executor"
        app.action_toggle_role()
        assert app.role == "orchestrator"
    app.exit()


@pytest.mark.asyncio
async def test_help_slash_command() -> None:
    app = AntigonaConsole(gateway_url="http://127.0.0.1:9", token="x")
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#chat_input").focus()
        await pilot.press("/", "h", "e", "l", "p", "enter")
        await pilot.pause()
        log = app.query_one("#chat_log", Log)
        text = "".join(str(line) for line in log.lines)
        assert "commands:" in text
    app.exit()


def test_helpers() -> None:
    assert _clip("x" * 100, 10) == "x" * 9 + "…"
    row = _event_row(1, {"type": "transition", "payload": {"to_state": "DONE"}})
    assert row[0] == "1"
    assert row[1] == "transition"
    assert status_color("done") == "green"
    assert status_color("running") == "blue"
