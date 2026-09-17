"""State-Driven Cancellable Portrait Animation Controller & Ticker (Phase L8 & L9).

Provides pure, generation-tokened frame progression, deterministic transitions,
and prompt_toolkit integration with zero fake semantic timers and single-task background lifecycle safety.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from antigona.cli_ui.portrait_engine import PortraitState
from antigona.cli_ui.portrait_frames import (
    PortraitProfile,
    get_portrait_frames,
)
from antigona.cli_ui.portrait_transitions import PortraitTransitionController

logger = logging.getLogger(__name__)

#: State-driven frame intervals in seconds (FPS = 1 / interval)
_STATE_INTERVALS: Final[Mapping[PortraitState, float]] = {
    PortraitState.THINKING: 0.30,      # ~3.3 FPS
    PortraitState.WORKING: 0.25,       # 4.0 FPS
    PortraitState.WAITING: 0.50,       # 2.0 FPS
    PortraitState.SPEAKING: 0.25,      # 4.0 FPS
    PortraitState.ACKNOWLEDGE: 0.15,   # ~6.6 FPS
    PortraitState.SUCCESS: 0.25,       # 4.0 FPS
    PortraitState.ERROR: 0.25,         # 4.0 FPS
    PortraitState.IDLE: 3.00,          # Low FPS idle breathing
    PortraitState.SLEEPING: 4.00,      # Very low FPS
}
_TRANSITION_INTERVAL: Final[float] = 0.15  # Fast 150ms per transition keyframe
_DEFAULT_INTERVAL: Final[float] = 0.50


@dataclass(slots=True)
class PortraitAnimationController:
    """Pure state controller for portrait animation sequences and transitions."""

    state: PortraitState = PortraitState.IDLE
    profile: PortraitProfile = PortraitProfile.MICRO
    frame_index: int = 0
    generation: int = 0
    transition_controller: PortraitTransitionController = field(
        default_factory=PortraitTransitionController
    )

    _transition_frames: tuple[str, ...] = field(default=(), init=False)
    _transition_index: int = field(default=0, init=False)
    _in_transition: bool = field(default=False, init=False)

    def set_state(self, new_state: PortraitState) -> bool:
        """Update semantic state. Returns True if state changed."""
        if self.state == new_state:
            return False

        old_state = self.state
        self.state = new_state
        self.frame_index = 0
        self.generation += 1

        # Compute deterministic transition sequence for (old_state -> new_state)
        trans_seq = self.transition_controller.get_transition_sequence(
            old_state, new_state, self.profile
        )
        if trans_seq:
            self._transition_frames = trans_seq
            self._transition_index = 0
            self._in_transition = True
        else:
            self._transition_frames = ()
            self._transition_index = 0
            self._in_transition = False

        return True

    def set_profile(self, new_profile: PortraitProfile) -> bool:
        """Update display profile. Returns True if profile changed."""
        if self.profile == new_profile:
            return False
        self.profile = new_profile
        self.frame_index = 0
        self.generation += 1
        # Safely cancel active transition on layout resize
        self._in_transition = False
        self._transition_frames = ()
        self._transition_index = 0
        return True

    def get_frame_count(self) -> int:
        """Return total frame count for current profile and state."""
        return len(get_portrait_frames(self.profile, self.state))

    def get_current_override_frame(self) -> str | None:
        """Return current transition keyframe if in active transition, or None."""
        if self._in_transition and self._transition_frames:
            idx = min(self._transition_index, len(self._transition_frames) - 1)
            return self._transition_frames[idx]
        return None

    def advance(self) -> bool:
        """Advance animation or transition sequence by one step.

        Returns True if the rendered content changed (requiring UI invalidate).
        """
        if self._in_transition and self._transition_frames:
            self._transition_index += 1
            if self._transition_index >= len(self._transition_frames):
                self._in_transition = False
                self._transition_frames = ()
                self._transition_index = 0
                self.frame_index = 0
            return True

        count = self.get_frame_count()
        if count <= 1:
            if self.frame_index != 0:
                self.frame_index = 0
                return True
            return False

        old_idx = self.frame_index
        self.frame_index = (old_idx + 1) % count
        return self.frame_index != old_idx

    def interval_for_current_state(self) -> float:
        """Return the target frame delay in seconds for the current state or transition."""
        if self._in_transition:
            return _TRANSITION_INTERVAL
        return _STATE_INTERVALS.get(self.state, _DEFAULT_INTERVAL)


@dataclass(slots=True)
class PortraitAnimationTicker:
    """Async background ticker for prompt_toolkit layout repaints."""

    controller: PortraitAnimationController
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    @property
    def is_running(self) -> bool:
        """Return True if background animation task is running."""
        return self._task is not None and not self._task.done()

    def start(self, app: Any) -> None:
        """Start the background animation ticker if not already running."""
        if self.is_running:
            return
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(
                self._run_loop(app), name="antigona-portrait-ticker"
            )
        except RuntimeError:
            logger.debug("No active event loop for portrait ticker start")

    def stop(self) -> None:
        """Synchronously cancel background animation task if running."""
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            self._task = None

    async def stop_async(self) -> None:
        """Async stop with await task cancellation cleanup."""
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def _run_loop(self, app: Any) -> None:
        """Main animation ticker loop."""
        try:
            while True:
                interval = self.controller.interval_for_current_state()
                await asyncio.sleep(interval)

                gen_before = self.controller.generation
                changed = self.controller.advance()

                # Stale frame protection: only invalidate if generation remained unchanged during sleep
                if changed and self.controller.generation == gen_before:
                    if app is not None and hasattr(app, "invalidate"):
                        app.invalidate()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Portrait animation loop terminated: %s", exc)
