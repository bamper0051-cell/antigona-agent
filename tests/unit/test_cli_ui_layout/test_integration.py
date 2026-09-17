"""Integration tests for the Antigona CLI layout with chat controller."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

import antigona.cli_ui.layout as layout_module
from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatMessage, ChatMessageRole, ChatUIState
from antigona.cli_ui.renderer import CliRenderer


@pytest.fixture
def mock_state():
    """Create a mock ChatUIState for testing."""
    return ChatUIState(
        messages=[],
        current_status="idle",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="test-session"
    )


@pytest.fixture
def mock_renderer():
    """Create a mock CliRenderer for testing."""
    return Mock(spec=CliRenderer)


@pytest.fixture
def layout(mock_state, mock_renderer):
    """Create an AntigonaLayout instance for testing."""
    async def mock_on_input(text: str) -> None:
        pass

    return AntigonaLayout(
        state=mock_state,
        renderer=mock_renderer,
        on_input=mock_on_input
    )


@pytest.fixture(autouse=True)
def fixed_terminal():
    """Deterministic terminal geometry: 80x24."""
    with patch.object(layout_module, "_terminal_size", return_value=(80, 24)):
        yield


def test_layout_updates_with_state_changes(layout):
    """Test that layout updates correctly when state changes."""
    assert layout.state.current_status == "idle"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "IDLE"

    layout.state.current_status = "planning"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "PLANNING"

    layout.state.current_status = "tool_executing"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "RUNNING_TOOL"

    layout.state.current_status = "verifying"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "VERIFYING"

    layout.state.current_status = "done"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "SUCCESS"

    layout.state.current_status = "failed"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "ERROR"


def test_layout_tracks_events(layout):
    """Events accumulate into the badge only while the viewport is frozen."""
    assert layout.new_events_indicator == 0
    assert layout.last_event_count == 0

    layout.state.events = [
        {"event": "plan", "detail": "planning phase"},
        {"event": "tool", "detail": "executing tool"},
    ]
    # Auto-follow: events are consumed, no badge.
    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 0
    assert layout.last_event_count == 2

    # Frozen: the next batch accumulates.
    layout.auto_follow = False
    layout.state.events.append({"event": "verify", "detail": "verifying results"})
    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 1
    assert layout.last_event_count == 3

    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 1


def test_layout_header_content_generation(layout):
    """Header shows the static face, status, connection and frozen badge."""
    layout.state.current_status = "planning"
    layout.state.connection = "connected"
    layout.state.active_flow_id = "4ab82f74abcd"
    layout._update_antigona_face_state()
    layout._update_new_events_indicator()

    fragments = layout._create_header().content.text()  # callable → (style, text) list
    header = "".join(text for _style, text in fragments)
    assert "[◎ ◎]" in header
    assert "[планирование]" in header
    assert "🟢 подключено" in header
    assert "задача 4ab82f74abcd" in header

    # Every fragment is a valid (style, text) pair; coloured ones carry a
    # real prompt_toolkit "fg:#RRGGBB" style token, not markup or an escape.
    for style, _text in fragments:
        assert isinstance(style, str)
        if style:
            assert style.startswith("fg:#") or "bold" in style
            assert "\x1b" not in style  # never a raw ANSI escape, always a style string

    # Frozen viewport with new events → badge in the header.
    layout.auto_follow = False
    layout.new_message_count = 2
    assert "↓2 новых" in layout._header_extra()


def test_layout_scroll_functions_work(layout):
    """Scroll up freezes; page down to the bottom resumes auto-follow."""
    assert layout.scroll_offset == 0
    assert layout.auto_follow is True

    layout.scroll_line_up()
    assert layout.scroll_offset == 0  # Nothing to scroll yet

    for i in range(30):
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content=f"m{i}"))
    layout.update_from_state()

    layout.scroll_line_up()
    assert layout.auto_follow is False
    assert layout.scroll_offset == 1

    layout.scroll_line_down()
    assert layout.auto_follow is True
    assert layout.scroll_offset == 0

    layout.scroll_page_up()
    assert layout.auto_follow is False
    layout.scroll_to_top()
    assert layout.scroll_offset == layout.max_scroll
    assert layout.max_scroll > 0

    layout.scroll_to_bottom()
    assert layout.auto_follow is True
    assert layout.scroll_offset == 0


if __name__ == "__main__":
    pytest.main([__file__])
