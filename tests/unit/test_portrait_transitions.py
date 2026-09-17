"""Unit tests for Portrait Transitions (Phase L9)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from rich.cells import cell_len

from antigona.cli_ui.portrait_animation import (
    PortraitAnimationController,
    PortraitAnimationTicker,
)
from antigona.cli_ui.portrait_engine import PortraitState
from antigona.cli_ui.portrait_frames import PortraitProfile
from antigona.cli_ui.portrait_transitions import (
    PortraitTransitionController,
    StateCategory,
    categorize_state,
)


def test_categorize_states() -> None:
    assert categorize_state(PortraitState.IDLE) == StateCategory.CLASSICAL
    assert categorize_state(PortraitState.TRACKING_INPUT) == StateCategory.CLASSICAL
    assert categorize_state(PortraitState.THINKING) == StateCategory.LIGHT_DIGITAL
    assert categorize_state(PortraitState.WORKING) == StateCategory.ACTIVE_DIGITAL
    assert categorize_state(PortraitState.SUCCESS) == StateCategory.TERMINAL_POSITIVE
    assert categorize_state(PortraitState.ERROR) == StateCategory.TERMINAL_NEGATIVE


def test_deterministic_transition_sequences() -> None:
    controller = PortraitTransitionController()

    seq1 = controller.get_transition_sequence(
        PortraitState.IDLE, PortraitState.THINKING, PortraitProfile.FULL
    )
    seq2 = controller.get_transition_sequence(
        PortraitState.IDLE, PortraitState.THINKING, PortraitProfile.FULL
    )

    assert len(seq1) == 1
    assert seq1 == seq2  # Deterministic equality


def test_full_transition_geometry() -> None:
    controller = PortraitTransitionController()

    transitions = [
        (PortraitState.IDLE, PortraitState.THINKING),
        (PortraitState.THINKING, PortraitState.WORKING),
        (PortraitState.WORKING, PortraitState.SUCCESS),
        (PortraitState.WORKING, PortraitState.ERROR),
    ]

    for from_st, to_st in transitions:
        seq = controller.get_transition_sequence(from_st, to_st, PortraitProfile.FULL)
        for frame in seq:
            lines = frame.splitlines()
            assert len(lines) == 14, f"FULL transition frame height != 14 for {from_st}->{to_st}"
            for line in lines:
                assert cell_len(line) == 46, f"FULL line width != 46 in {from_st}->{to_st}"


def test_compact_transition_geometry() -> None:
    controller = PortraitTransitionController()

    transitions = [
        (PortraitState.IDLE, PortraitState.THINKING),
        (PortraitState.THINKING, PortraitState.WORKING),
        (PortraitState.WORKING, PortraitState.SUCCESS),
    ]

    for from_st, to_st in transitions:
        seq = controller.get_transition_sequence(from_st, to_st, PortraitProfile.COMPACT)
        for frame in seq:
            lines = frame.splitlines()
            assert len(lines) == 6, f"COMPACT transition frame height != 6 for {from_st}->{to_st}"
            for line in lines:
                assert cell_len(line) == 22, f"COMPACT line width != 22 in {from_st}->{to_st}"


def test_rapid_state_change_latest_state_wins() -> None:
    anim = PortraitAnimationController()
    anim.set_profile(PortraitProfile.FULL)

    # 1. Transition IDLE -> THINKING
    anim.set_state(PortraitState.THINKING)
    assert anim._in_transition is True
    override1 = anim.get_current_override_frame()
    assert override1 is not None

    # 2. Immediate rapid change THINKING -> WORKING
    anim.set_state(PortraitState.WORKING)
    assert anim.state == PortraitState.WORKING
    assert anim._in_transition is True

    override2 = anim.get_current_override_frame()
    assert override2 is not None
    assert override2 != override1

    # Advance until transition completes
    anim.advance()
    assert anim._in_transition is False
    assert anim.get_current_override_frame() is None
    assert anim.state == PortraitState.WORKING


def test_profile_resize_cancels_active_transition_safely() -> None:
    anim = PortraitAnimationController()
    anim.set_profile(PortraitProfile.FULL)
    anim.set_state(PortraitState.THINKING)

    assert anim._in_transition is True

    # Terminal resize during transition
    anim.set_profile(PortraitProfile.COMPACT)
    assert anim._in_transition is False
    assert anim.get_current_override_frame() is None
    assert anim.profile == PortraitProfile.COMPACT


def test_transition_never_mutates_semantic_state() -> None:
    anim = PortraitAnimationController()
    anim.set_profile(PortraitProfile.FULL)
    anim.set_state(PortraitState.THINKING)

    assert anim.state == PortraitState.THINKING
    anim.advance()
    assert anim.state == PortraitState.THINKING


@pytest.mark.asyncio
async def test_transition_completes_and_returns_control_to_state_loop() -> None:
    anim = PortraitAnimationController()
    anim.set_profile(PortraitProfile.FULL)
    anim.set_state(PortraitState.THINKING)

    ticker = PortraitAnimationTicker(controller=anim)
    mock_app = MagicMock()

    ticker.start(mock_app)
    await asyncio.sleep(0.35)

    assert anim._in_transition is False
    assert mock_app.invalidate.called

    await ticker.stop_async()
