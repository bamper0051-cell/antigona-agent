"""Regression tests: /exit and Ctrl+C must actually stop AntigonaLayout.

Bug: accept_handler fired on_input() as a detached background task and
never inspected the CommandDisposition it returned, so /exit (and /quit)
did nothing in the live TUI even though ChatController.handle_input()
correctly classified them as CommandKind.EXIT. Ctrl+C had no key binding
at all. Both are fixed by intercepting /exit before dispatch and by
binding "c-c" to Application.exit().
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatUIState
from antigona.cli_ui.renderer import CliRenderer


@pytest.fixture
def mock_state() -> ChatUIState:
    return ChatUIState(
        messages=[],
        current_status="idle",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="test-session",
    )


@pytest.fixture
def mock_renderer() -> Mock:
    return Mock(spec=CliRenderer)


def _make_layout(mock_state: ChatUIState, mock_renderer: Mock) -> tuple[AntigonaLayout, list[str]]:
    calls: list[str] = []

    async def on_input(text: str) -> None:
        calls.append(text)

    layout = AntigonaLayout(state=mock_state, renderer=mock_renderer, on_input=on_input)
    return layout, calls


def test_exit_stops_application_without_dispatching(mock_state, mock_renderer) -> None:
    layout, calls = _make_layout(mock_state, mock_renderer)
    layout.application = Mock()

    layout._handle_submit("/exit")

    layout.application.exit.assert_called_once()
    assert calls == []


def test_quit_stops_application_without_dispatching(mock_state, mock_renderer) -> None:
    layout, calls = _make_layout(mock_state, mock_renderer)
    layout.application = Mock()

    layout._handle_submit("/quit")

    layout.application.exit.assert_called_once()
    assert calls == []


@pytest.mark.asyncio
async def test_regular_text_still_dispatches_to_on_input(mock_state, mock_renderer) -> None:
    layout, calls = _make_layout(mock_state, mock_renderer)
    layout.application = Mock()

    layout._handle_submit("привет")
    # on_input() is fired via asyncio.create_task(); let it run.
    await asyncio.sleep(0)

    layout.application.exit.assert_not_called()
    assert calls == ["привет"]


def test_blank_input_does_nothing(mock_state, mock_renderer) -> None:
    layout, calls = _make_layout(mock_state, mock_renderer)
    layout.application = Mock()

    layout._handle_submit("   ")

    layout.application.exit.assert_not_called()
    assert calls == []


def test_ctrl_c_is_bound_and_exits(mock_state, mock_renderer) -> None:
    layout, _calls = _make_layout(mock_state, mock_renderer)

    binding = next(
        (b for b in layout.kb.bindings if tuple(k.value if hasattr(k, "value") else k for k in b.keys) == ("c-c",)),
        None,
    )
    assert binding is not None, "c-c must be bound"

    fake_event = Mock()
    fake_event.app = Mock()
    binding.handler(fake_event)
    fake_event.app.exit.assert_called_once()
