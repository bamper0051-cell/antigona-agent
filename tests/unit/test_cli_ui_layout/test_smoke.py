"""Smoke tests for the Antigona CLI layout."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatUIState
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


def test_layout_can_be_created(mock_state, mock_renderer):
    """Test that layout can be instantiated."""
    async def mock_on_input(text: str) -> None:
        pass
    
    layout = AntigonaLayout(
        state=mock_state,
        renderer=mock_renderer,
        on_input=mock_on_input
    )
    
    assert layout is not None
    assert layout.state == mock_state
    assert layout.renderer == mock_renderer


def test_layout_has_required_methods(mock_state, mock_renderer):
    """Test that layout has all required methods."""
    async def mock_on_input(text: str) -> None:
        pass
    
    layout = AntigonaLayout(
        state=mock_state,
        renderer=mock_renderer,
        on_input=mock_on_input
    )
    
    # Check that all required methods exist
    assert hasattr(layout, '_setup_key_bindings')
    assert hasattr(layout, '_create_header')
    assert hasattr(layout, '_create_center')
    assert hasattr(layout, '_create_input')
    assert hasattr(layout, '_create_layout')
    assert hasattr(layout, '_setup_application')
    assert hasattr(layout, 'scroll_line_up')
    assert hasattr(layout, 'scroll_line_down')
    assert hasattr(layout, 'scroll_page_up')
    assert hasattr(layout, 'scroll_page_down')
    assert hasattr(layout, 'scroll_to_top')
    assert hasattr(layout, 'scroll_to_bottom')
    assert hasattr(layout, '_update_center_content')
    assert hasattr(layout, 'run')
    assert hasattr(layout, 'stop')
    # New methods we added
    assert hasattr(layout, 'update_from_state')
    assert hasattr(layout, '_update_antigona_face_state')
    assert hasattr(layout, '_update_new_events_indicator')
    assert hasattr(layout, '_get_antigona_face')
    assert hasattr(layout, '_request_owner_pin')


def test_layout_state_updates_work(mock_state, mock_renderer):
    """Test that layout state updates work correctly."""
    async def mock_on_input(text: str) -> None:
        pass
    
    layout = AntigonaLayout(
        state=mock_state,
        renderer=mock_renderer,
        on_input=mock_on_input
    )
    
    # Test initial state
    assert layout.state.current_status == "idle"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "IDLE"
    
    # Test state change
    layout.state.current_status = "planning"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "PLANNING"
    
    # Test events tracking (badge only accumulates while the viewport is frozen)
    assert layout.new_events_indicator == 0
    layout.auto_follow = False
    layout.state.events = [{"event": "test"}]
    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 1


def test_layout_scroll_functions_work(mock_state, mock_renderer):
    """Test that layout scroll functions work."""
    async def mock_on_input(text: str) -> None:
        pass
    
    layout = AntigonaLayout(
        state=mock_state,
        renderer=mock_renderer,
        on_input=mock_on_input
    )
    
    # Test scroll functions don't crash
    layout.scroll_line_up()
    layout.scroll_line_down()
    layout.scroll_page_up()
    layout.scroll_page_down()
    layout.scroll_to_top()
    layout.scroll_to_bottom()
    
    # Just verify they don't raise exceptions
    assert True


if __name__ == "__main__":
    pytest.main([__file__])