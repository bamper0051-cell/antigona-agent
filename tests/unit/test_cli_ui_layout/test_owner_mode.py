"""Tests for the Owner Mode PIN authentication in Antigona CLI layout."""

from __future__ import annotations

import os
import sys
from unittest.mock import Mock, patch

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


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason='NoConsoleScreenBufferError: prompt_toolkit needs a Windows console, unavailable under pytest (Wave 4)')
async def test_request_owner_pin_no_pin_configured(layout):
    """Test that Owner Mode is granted when no PIN is configured."""
    # Ensure no PIN is configured
    with patch.dict(os.environ, {}, clear=True):
        assert not os.environ.get("ANTIGONA_PIN")
        
        # Request owner PIN
        await layout._request_owner_pin()
        
        # Should be granted access
        assert layout.owner_mode is True


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason='NoConsoleScreenBufferError: prompt_toolkit needs a Windows console, unavailable under pytest (Wave 4)')
async def test_request_owner_pin_correct_pin(layout):
    """Test that Owner Mode is granted with correct PIN."""
    # Set a test PIN
    test_pin = "1234"
    with patch.dict(os.environ, {"ANTIGONA_PIN": test_pin}):
        # Mock the read_prompt function to return the correct PIN
        with patch('antigona.cli_ui.layout.read_prompt', return_value=test_pin) as mock_read:
            # Request owner PIN
            await layout._request_owner_pin()
            
            # Should be granted access
            assert layout.owner_mode is True
            assert layout.pin_attempts == 0  # Reset on success
            mock_read.assert_called_once()
            # PIN input must be hidden (is_password=True) to avoid plaintext leak
            kwargs = mock_read.call_args.kwargs
            assert kwargs.get("is_password") is True
            assert kwargs.get("prompt_str") is not None
            assert kwargs.get("session") is not None


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason='NoConsoleScreenBufferError: prompt_toolkit needs a Windows console, unavailable under pytest (Wave 4)')
async def test_request_owner_pin_incorrect_then_correct(layout):
    """Test that Owner Mode is granted after incorrect then correct PIN."""
    # Set a test PIN
    test_pin = "1234"
    with patch.dict(os.environ, {"ANTIGONA_PIN": test_pin}):
        # Mock the read_prompt function to return incorrect then correct PIN
        with patch('antigona.cli_ui.layout.read_prompt', side_effect=["0000", "", test_pin]) as mock_read:
            # Request owner PIN
            await layout._request_owner_pin()
            
            # Should be granted access
            assert layout.owner_mode is True
            assert layout.pin_attempts == 0  # Reset on success
            assert mock_read.call_count == 3  # 2 PIN prompts + 1 error prompt


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason='NoConsoleScreenBufferError: prompt_toolkit needs a Windows console, unavailable under pytest (Wave 4)')
async def test_request_owner_pin_max_attempts_exceeded(layout):
    """Test that access is denied after max PIN attempts exceeded."""
    # Set a test PIN
    test_pin = "1234"
    with patch.dict(os.environ, {"ANTIGONA_PIN": test_pin}):
        # Mock the read_prompt function to always return incorrect PIN
        with patch('antigona.cli_ui.layout.read_prompt', return_value="0000") as mock_read:
            # Request owner PIN
            await layout._request_owner_pin()
            
            # Should have exited due to max attempts exceeded
            # Since we can't easily test sys.exit() in a unit test,
            # we'll verify that owner_mode is still False and attempts were made
            assert layout.owner_mode is False
            assert layout.pin_attempts == layout.max_pin_attempts
            # Each attempt triggers PIN prompt + error prompt, so count >= attempts
            assert mock_read.call_count >= layout.max_pin_attempts


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason='NoConsoleScreenBufferError: prompt_toolkit needs a Windows console, unavailable under pytest (Wave 4)')
async def test_request_owner_pin_keyboard_interrupt(layout):
    """Test that KeyboardInterrupt exits the PIN request."""
    # Set a test PIN
    test_pin = "1234"
    with patch.dict(os.environ, {"ANTIGONA_PIN": test_pin}):
        # Mock the read_prompt function to raise KeyboardInterrupt
        with patch('antigona.cli_ui.layout.read_prompt', side_effect=KeyboardInterrupt):
            # Request owner PIN
            await layout._request_owner_pin()
            
            # Should have exited due to KeyboardInterrupt
            # Since we can't easily test sys.exit() in a unit test,
            # we'll verify that owner_mode is still False
            assert layout.owner_mode is False


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason='NoConsoleScreenBufferError: prompt_toolkit needs a Windows console, unavailable under pytest (Wave 4)')
async def test_request_owner_pin_eof_error(layout):
    """Test that EOFError exits the PIN request."""
    # Set a test PIN
    test_pin = "1234"
    with patch.dict(os.environ, {"ANTIGONA_PIN": test_pin}):
        # Mock the read_prompt function to raise EOFError
        with patch('antigona.cli_ui.layout.read_prompt', side_effect=EOFError):
            # Request owner PIN
            await layout._request_owner_pin()
            
            # Should have exited due to EOFError
            # Since we can't easily test sys.exit() in a unit test,
            # we'll verify that owner_mode is still False
            assert layout.owner_mode is False


def test_layout_update_from_state(layout):
    """Test that update_from_state calls the expected methods."""
    # Mock the internal methods
    with patch.object(layout, '_update_antigona_face_state') as mock_face, \
         patch.object(layout, '_update_new_events_indicator') as mock_events:
        
        # Call update_from_state
        layout.update_from_state()
        
        # Verify the methods were called
        mock_face.assert_called_once()
        mock_events.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__])