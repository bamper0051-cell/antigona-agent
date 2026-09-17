"""Unit tests for Portrait Animation Controller & Ticker (Phase L8)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from antigona.cli_ui.portrait_animation import (
    PortraitAnimationController,
    PortraitAnimationTicker,
)
from antigona.cli_ui.portrait_engine import PortraitState
from antigona.cli_ui.portrait_frames import PortraitProfile


def test_controller_initial_state() -> None:
    controller = PortraitAnimationController()
    assert controller.state == PortraitState.IDLE
    assert controller.profile == PortraitProfile.MICRO
    assert controller.frame_index == 0
    assert controller.generation == 0


def test_set_state_resets_index_and_increments_generation() -> None:
    controller = PortraitAnimationController()
    controller.frame_index = 1

    changed = controller.set_state(PortraitState.THINKING)
    assert changed is True
    assert controller.state == PortraitState.THINKING
    assert controller.frame_index == 0
    assert controller.generation == 1

    # Setting same state returns False and leaves generation unchanged
    changed_again = controller.set_state(PortraitState.THINKING)
    assert changed_again is False
    assert controller.generation == 1


def test_set_profile_resets_index_and_increments_generation() -> None:
    controller = PortraitAnimationController()
    controller.set_state(PortraitState.THINKING)
    controller.frame_index = 1

    changed = controller.set_profile(PortraitProfile.FULL)
    assert changed is True
    assert controller.profile == PortraitProfile.FULL
    assert controller.frame_index == 0
    assert controller.generation == 2


def test_advance_multi_frame_sequence() -> None:
    controller = PortraitAnimationController()
    controller.set_profile(PortraitProfile.FULL)
    controller.set_state(PortraitState.THINKING)

    count = controller.get_frame_count()
    assert count == 2

    # Advance past transition keyframe to start main state sequence
    if controller._in_transition:
        controller.advance()

    assert controller.frame_index == 0
    changed1 = controller.advance()
    assert changed1 is True
    assert controller.frame_index == 1

    changed2 = controller.advance()
    assert changed2 is True
    assert controller.frame_index == 0


def test_advance_static_single_frame() -> None:
    controller = PortraitAnimationController()
    controller.set_profile(PortraitProfile.MICRO)
    controller.set_state(PortraitState.IDLE)

    count = controller.get_frame_count()
    assert count == 1

    changed = controller.advance()
    assert changed is False
    assert controller.frame_index == 0


def test_stale_frame_protection_generation_check() -> None:
    controller = PortraitAnimationController()
    controller.set_profile(PortraitProfile.FULL)
    controller.set_state(PortraitState.THINKING)

    gen_before = controller.generation
    controller.set_state(PortraitState.WORKING)
    gen_after = controller.generation

    assert gen_after > gen_before
    assert controller.frame_index == 0


@pytest.mark.asyncio
async def test_ticker_single_active_owner() -> None:
    controller = PortraitAnimationController()
    ticker = PortraitAnimationTicker(controller=controller)
    mock_app = MagicMock()

    ticker.start(mock_app)
    assert ticker.is_running is True

    task1 = ticker._task

    # Repeated start call does not spawn a second loop
    ticker.start(mock_app)
    assert ticker._task is task1

    await ticker.stop_async()
    assert ticker.is_running is False
    assert ticker._task is None


@pytest.mark.asyncio
async def test_ticker_calls_invalidate_on_frame_change() -> None:
    controller = PortraitAnimationController()
    controller.set_profile(PortraitProfile.FULL)
    controller.set_state(PortraitState.THINKING)  # 2 frames

    ticker = PortraitAnimationTicker(controller=controller)
    mock_app = MagicMock()

    ticker.start(mock_app)
    await asyncio.sleep(0.35)

    assert mock_app.invalidate.called

    await ticker.stop_async()


@pytest.mark.asyncio
async def test_ticker_does_not_invalidate_on_static_frame() -> None:
    controller = PortraitAnimationController()
    controller.set_profile(PortraitProfile.MICRO)
    controller.set_state(PortraitState.IDLE)  # 1 frame

    ticker = PortraitAnimationTicker(controller=controller)
    mock_app = MagicMock()

    ticker.start(mock_app)
    await asyncio.sleep(0.1)

    # Single frame state -> advance() returns False -> no invalidate call
    assert not mock_app.invalidate.called

    await ticker.stop_async()
