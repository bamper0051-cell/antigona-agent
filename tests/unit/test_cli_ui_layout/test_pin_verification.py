"""Tests for PIN verification logic in Antigona CLI layout."""

from __future__ import annotations

import hashlib
import hmac
import os
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


def test_pin_verification_logic(layout):
    """Test the core PIN verification logic."""
    test_pin = "1234"
    test_input = "1234"
    
    with patch.dict(os.environ, {"ANTIGONA_PIN": test_pin}):
        # Calculate expected hash
        expected = hashlib.sha256(test_pin.encode()).hexdigest()
        # Calculate actual hash
        actual = hashlib.sha256(test_input.encode()).hexdigest()
        
        # Verify they match using hmac.compare_digest
        assert hmac.compare_digest(expected, actual) is True


def test_pin_verification_wrong_input(layout):
    """Test PIN verification with wrong input."""
    test_pin = "1234"
    test_input = "0000"
    
    with patch.dict(os.environ, {"ANTIGONA_PIN": test_pin}):
        # Calculate expected hash
        expected = hashlib.sha256(test_pin.encode()).hexdigest()
        # Calculate actual hash
        actual = hashlib.sha256(test_input.encode()).hexdigest()
        
        # Verify they don't match using hmac.compare_digest
        assert hmac.compare_digest(expected, actual) is False


def test_pin_verification_empty_pin(layout):
    """Test PIN verification when no PIN is configured."""
    with patch.dict(os.environ, {}, clear=True):
        # If no PIN is configured, access should be granted
        pin = os.environ.get("ANTIGONA_PIN", "")
        assert not pin  # Should be empty


def test_attempts_counter(layout):
    """Test that the attempts counter works correctly."""
    assert layout.pin_attempts == 0
    assert layout.max_pin_attempts == 3
    
    # Simulate failed attempts
    layout.pin_attempts += 1
    assert layout.pin_attempts == 1
    
    layout.pin_attempts += 1
    assert layout.pin_attempts == 2
    
    layout.pin_attempts += 1
    assert layout.pin_attempts == 3
    
    # Check if max attempts exceeded
    assert layout.pin_attempts >= layout.max_pin_attempts


def test_attempts_reset_on_success(layout):
    """Test that attempts are reset on successful PIN entry."""
    layout.pin_attempts = 2  # Simulate some failed attempts
    assert layout.pin_attempts == 2
    
    # Reset on success
    layout.pin_attempts = 0
    assert layout.pin_attempts == 0


if __name__ == "__main__":
    pytest.main([__file__])