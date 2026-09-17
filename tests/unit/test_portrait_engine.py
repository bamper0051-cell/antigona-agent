"""Unit tests for Antigona CLI UI Portrait Engine."""

from __future__ import annotations

from rich.cells import cell_len

from antigona.cli_ui.portrait_engine import (
    PortraitController,
    PortraitState,
    gaze_state,
    get_portrait_glyph,
    portrait_state_for_status,
)


def test_portrait_state_enum() -> None:
    assert len(PortraitState) == 13
    assert PortraitState.IDLE == "IDLE"
    assert PortraitState.THINKING == "THINKING"
    assert PortraitState.WORKING == "WORKING"
    assert PortraitState.SUCCESS == "SUCCESS"
    assert PortraitState.ERROR == "ERROR"


def test_gaze_state_mapping() -> None:
    # Empty input -> IDLE
    assert gaze_state("", 0) == PortraitState.IDLE

    # Cursor at start -> LOOK_DOWN_LEFT (ratio 0.0 < 0.33)
    assert gaze_state("hello world", 0) == PortraitState.LOOK_DOWN_LEFT

    # Cursor in middle -> LOOK_DOWN_CENTER (ratio 5/11 ~ 0.45)
    assert gaze_state("hello world", 5) == PortraitState.LOOK_DOWN_CENTER

    # Cursor at end -> LOOK_DOWN_RIGHT (ratio 11/11 == 1.0 > 0.66)
    assert gaze_state("hello world", 11) == PortraitState.LOOK_DOWN_RIGHT

    # Clamping tests
    assert gaze_state("abc", -10) == PortraitState.LOOK_DOWN_LEFT
    assert gaze_state("abc", 100) == PortraitState.LOOK_DOWN_RIGHT


def test_portrait_controller_transitions() -> None:
    ctrl = PortraitController()
    assert ctrl.state == PortraitState.IDLE

    # Typing updates gaze
    ctrl.on_typing("hello", 1)
    assert ctrl.state == PortraitState.LOOK_DOWN_LEFT

    # Submit -> ACKNOWLEDGE
    ctrl.on_submit()
    assert ctrl.state == PortraitState.ACKNOWLEDGE

    # Active agent state wins over typing
    ctrl.set_state(PortraitState.WORKING)
    assert ctrl.state == PortraitState.WORKING
    ctrl.on_typing("typing while working", 2)
    assert ctrl.state == PortraitState.WORKING  # Remains WORKING

    # Force idle resets state
    ctrl.force_idle()
    assert ctrl.state == PortraitState.IDLE


def test_portrait_state_for_status() -> None:
    assert portrait_state_for_status("idle") == PortraitState.IDLE
    assert portrait_state_for_status("sending") == PortraitState.THINKING
    assert portrait_state_for_status("planning") == PortraitState.THINKING
    assert portrait_state_for_status("tool_executing") == PortraitState.WORKING
    assert portrait_state_for_status("observing") == PortraitState.WORKING
    assert portrait_state_for_status("verifying") == PortraitState.WAITING
    assert portrait_state_for_status("waiting_approval") == PortraitState.WAITING
    assert portrait_state_for_status("done") == PortraitState.SUCCESS
    assert portrait_state_for_status("failed") == PortraitState.ERROR
    assert portrait_state_for_status("timeout") == PortraitState.ERROR
    assert portrait_state_for_status("unknown_status") == PortraitState.IDLE


def test_get_portrait_glyph_cell_width() -> None:
    for state in PortraitState:
        glyph = get_portrait_glyph(state)
        assert cell_len(glyph) == 5, f"Glyph for {state} must have width 5"
