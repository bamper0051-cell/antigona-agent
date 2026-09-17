"""Responsive Portrait Renderer for Antigona CLI UI.

Provides pure, height-and-width aware selection of PortraitProfile (FULL, COMPACT, MICRO)
and terminal-safe rendering of portrait frames without side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from antigona.cli_ui.portrait_engine import PortraitState
from antigona.cli_ui.portrait_frames import (
    PortraitProfile,
    get_portrait_frames,
)

#: Minimum terminal dimensions for FULL profile (46x14)
MIN_FULL_WIDTH: Final[int] = 100
MIN_FULL_HEIGHT: Final[int] = 28

#: Minimum terminal dimensions for COMPACT profile (22x6)
MIN_COMPACT_WIDTH: Final[int] = 60
MIN_COMPACT_HEIGHT: Final[int] = 20

#: Minimum conversation view height to preserve usability
MIN_CHAT_HEIGHT: Final[int] = 10

#: Reserved layout vertical space (1 line input + 1 line status + 2 margins)
RESERVED_LAYOUT_HEIGHT: Final[int] = 4


def choose_portrait_profile(columns: int, lines: int) -> PortraitProfile:
    """Deterministically select a PortraitProfile based on available terminal dimensions.

    Requires BOTH sufficient width and sufficient height so that conversation log and input line
    are never pushed off-screen or cramped.
    """
    usable_height = max(0, lines - RESERVED_LAYOUT_HEIGHT - MIN_CHAT_HEIGHT)

    if columns >= MIN_FULL_WIDTH and usable_height >= 14 and lines >= MIN_FULL_HEIGHT:
        return PortraitProfile.FULL
    if columns >= MIN_COMPACT_WIDTH and usable_height >= 6 and lines >= MIN_COMPACT_HEIGHT:
        return PortraitProfile.COMPACT
    return PortraitProfile.MICRO


@dataclass(slots=True)
class PortraitRenderer:
    """Pure presentation renderer for portrait frames."""

    def render(
        self,
        profile: PortraitProfile,
        state: PortraitState,
        frame_index: int = 0,
        override_frame: str | None = None,
    ) -> str:
        """Render a deterministic frame for the given profile and state."""
        if override_frame is not None:
            return override_frame
        frames = get_portrait_frames(profile, state)
        if not frames:
            # Fallback safely to MICRO idle glyph
            return "[◉ ◉]"
        idx = max(0, frame_index) % len(frames)
        return frames[idx]
