"""Deterministic Classical -> Digital Portrait Transitions for Antigona CLI UI (Phase L9).

Provides pure, cell-width validated intermediate transition frame sequences
between classical bust states, digital states, and terminal outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from antigona.cli_ui.portrait_engine import PortraitState
from antigona.cli_ui.portrait_frames import (
    PortraitProfile,
    format_frame,
    get_portrait_frames,
    validate_frame,
)


class StateCategory(StrEnum):
    CLASSICAL = "classical"
    LIGHT_DIGITAL = "light_digital"
    ACTIVE_DIGITAL = "active_digital"
    TERMINAL_POSITIVE = "terminal_positive"
    TERMINAL_NEGATIVE = "terminal_negative"


def categorize_state(state: PortraitState) -> StateCategory:
    """Categorize a PortraitState by visual digital intensity."""
    if state in {
        PortraitState.IDLE,
        PortraitState.TRACKING_INPUT,
        PortraitState.LOOK_DOWN_LEFT,
        PortraitState.LOOK_DOWN_CENTER,
        PortraitState.LOOK_DOWN_RIGHT,
        PortraitState.SLEEPING,
    }:
        return StateCategory.CLASSICAL
    if state in {PortraitState.ACKNOWLEDGE, PortraitState.THINKING}:
        return StateCategory.LIGHT_DIGITAL
    if state in {
        PortraitState.WORKING,
        PortraitState.WAITING,
        PortraitState.SPEAKING,
    }:
        return StateCategory.ACTIVE_DIGITAL
    if state == PortraitState.SUCCESS:
        return StateCategory.TERMINAL_POSITIVE
    return StateCategory.TERMINAL_NEGATIVE


# ── FULL PROFILE TRANSITION FRAMES (46x14) ───────────────────────────

_FULL_WIDTH: Final[int] = 46
_FULL_HEIGHT: Final[int] = 14

# Transition: Classical -> Light Digital (Frame 1: Initial digital particles)
_FULL_TRANS_CLASSICAL_TO_LIGHT_1 = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'  λ   '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ◉      ◉  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.  :: .'        /",
        r"           '.       '---'        .'",
        r"             '-.   < 01 >     .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

# Transition: Light Digital -> Active Digital (Frame 1: Structural code lines)
_FULL_TRANS_LIGHT_TO_ACTIVE_1 = format_frame(
    [
        r"              _{Σ}[01]/{Δ}\[π]_",
        r"           ./{01 10 0110 01 10}\.",
        r"         .{Σ}0110[ANTIGONA]101{Δ}.",
        r"        /01/{__1010101010__}\10\\",
        r"       /10[  |0101010101|  ]01\\",
        r"      |01   {10 10 01 01}    10|",
        r"      |10   [0]  {Δ}  [1]    01|",
        r"      |01      <  Σ  >        10|",
        r"      |10   {01__INIT__10}   01|",
        r"       \01   \1010==0101/   10/",
        r"        \10    \010110/    01/",
        r"         '{Σ}01__====__10{Δ}'",
        r"           ||  01 10 01  ||",
        r"         .-|| {starting} ||-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

# Transition: Active Digital -> Classical (Frame 1: Digital fade out)
_FULL_TRANS_ACTIVE_TO_CLASSICAL_1 = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'  ::  '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ◉      ◉  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.   [RESTORE]  .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

# Transition: Any -> Error Glitch (Frame 1: Short fault accent)
_FULL_TRANS_ANY_TO_ERROR_1 = format_frame(
    [
        r"             !!! [ SYSTEM FAULT ] !!!",
        r"          ./{0xA5}////FAULT\\{0xA5}\.",
        r"        .[ERROR][STATE DESYNC][ERROR].",
        r"       /!!/{__CRITICAL__}\!!\\",
        r"      /XX[  |XXXXXXXXXX|  ]XX\\",
        r"     |!!   [!!]  <X>  [!!]    !!|",
        r"     |XX      !!  Δ  !!        XX|",
        r"     |!!       <FAULT>          !!|",
        r"     |XX   [BROKEN SIGNAL]      XX|",
        r"      \!!   /XXXX==XXXX\      !!/",
        r"       \XX    /!X!X!\       XX/",
        r"        '[!!]__====__[!!]'",
        r"          ||  ERROR   ||",
        r"        .-|| RECOVER  ||-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)


# ── COMPACT PROFILE TRANSITION FRAMES (22x6) ─────────────────────────

_COMPACT_WIDTH: Final[int] = 22
_COMPACT_HEIGHT: Final[int] = 6

_COMPACT_TRANS_CLASSICAL_TO_LIGHT = format_frame(
    [
        r"    .------------.",
        r"   /  ◉   λ    ◉  \\",
        r"  |     < 01 >     |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_TRANS_LIGHT_TO_ACTIVE = format_frame(
    [
        r"   .==[  INIT  ]==.",
        r"  / 01  ◎    ◎  10 \\",
        r" |  [01]  Δ  [10]   |",
        r" |     < 010 >      |",
        r"  \ 1010==010110   /",
        r"   '=============='",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_TRANS_ACTIVE_TO_CLASSICAL = format_frame(
    [
        r"    .------------.",
        r"   /  ◉        ◉  \\",
        r"  |    [SYNC]      |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)


# Validate all transition frames at import time
for _fr, _w, _h in [
    (_FULL_TRANS_CLASSICAL_TO_LIGHT_1, _FULL_WIDTH, _FULL_HEIGHT),
    (_FULL_TRANS_LIGHT_TO_ACTIVE_1, _FULL_WIDTH, _FULL_HEIGHT),
    (_FULL_TRANS_ACTIVE_TO_CLASSICAL_1, _FULL_WIDTH, _FULL_HEIGHT),
    (_FULL_TRANS_ANY_TO_ERROR_1, _FULL_WIDTH, _FULL_HEIGHT),
    (_COMPACT_TRANS_CLASSICAL_TO_LIGHT, _COMPACT_WIDTH, _COMPACT_HEIGHT),
    (_COMPACT_TRANS_LIGHT_TO_ACTIVE, _COMPACT_WIDTH, _COMPACT_HEIGHT),
    (_COMPACT_TRANS_ACTIVE_TO_CLASSICAL, _COMPACT_WIDTH, _COMPACT_HEIGHT),
]:
    validate_frame(_fr, _w, _h)


@dataclass(slots=True)
class PortraitTransitionController:
    """Pure controller for computing transition frame sequences between PortraitStates."""

    def get_transition_sequence(
        self,
        from_state: PortraitState,
        to_state: PortraitState,
        profile: PortraitProfile,
    ) -> tuple[str, ...]:
        """Return a tuple of keyframe transition strings for (from_state -> to_state).

        Returns empty tuple if states belong to the same visual category.
        """
        cat_from = categorize_state(from_state)
        cat_to = categorize_state(to_state)

        if cat_from == cat_to:
            return ()

        # Micro profile uses single-frame state transitions without intermediate keyframes
        if profile == PortraitProfile.MICRO:
            target_frames = get_portrait_frames(profile, to_state)
            return (target_frames[0],) if target_frames else ()

        # Full profile transitions
        if profile == PortraitProfile.FULL:
            if cat_to == StateCategory.TERMINAL_NEGATIVE:
                return (_FULL_TRANS_ANY_TO_ERROR_1,)
            if (
                cat_from == StateCategory.CLASSICAL
                and cat_to == StateCategory.LIGHT_DIGITAL
            ):
                return (_FULL_TRANS_CLASSICAL_TO_LIGHT_1,)
            if (
                cat_from == StateCategory.LIGHT_DIGITAL
                and cat_to == StateCategory.ACTIVE_DIGITAL
            ):
                return (_FULL_TRANS_LIGHT_TO_ACTIVE_1,)
            if cat_from in {
                StateCategory.ACTIVE_DIGITAL,
                StateCategory.LIGHT_DIGITAL,
                StateCategory.TERMINAL_POSITIVE,
            } and cat_to == StateCategory.CLASSICAL:
                return (_FULL_TRANS_ACTIVE_TO_CLASSICAL_1,)

            # Fallback to initial frame of target state
            target_frames = get_portrait_frames(profile, to_state)
            return (target_frames[0],) if target_frames else ()

        # Compact profile transitions
        if cat_to == StateCategory.TERMINAL_NEGATIVE:
            target_frames = get_portrait_frames(profile, to_state)
            return (target_frames[0],) if target_frames else ()
        if (
            cat_from == StateCategory.CLASSICAL
            and cat_to == StateCategory.LIGHT_DIGITAL
        ):
            return (_COMPACT_TRANS_CLASSICAL_TO_LIGHT,)
        if (
            cat_from == StateCategory.LIGHT_DIGITAL
            and cat_to == StateCategory.ACTIVE_DIGITAL
        ):
            return (_COMPACT_TRANS_LIGHT_TO_ACTIVE,)
        if cat_from in {
            StateCategory.ACTIVE_DIGITAL,
            StateCategory.LIGHT_DIGITAL,
            StateCategory.TERMINAL_POSITIVE,
        } and cat_to == StateCategory.CLASSICAL:
            return (_COMPACT_TRANS_ACTIVE_TO_CLASSICAL,)

        target_frames = get_portrait_frames(profile, to_state)
        return (target_frames[0],) if target_frames else ()
