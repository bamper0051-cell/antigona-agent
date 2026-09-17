"""Deterministic responsive portrait frame library for Antigona CLI UI.

Provides cell-width validated ASCII and Unicode frame families across three
responsive profiles: FULL (46x14), COMPACT (22x6), and MICRO (5x1).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from rich.cells import cell_len

from antigona.cli_ui.portrait_engine import PortraitState


class PortraitProfile(StrEnum):
    FULL = "full"
    COMPACT = "compact"
    MICRO = "micro"


@dataclass(frozen=True, slots=True)
class PortraitFrameSet:
    profile: PortraitProfile
    width: int
    height: int
    frames: tuple[str, ...]


def _fit_and_pad_line(line: str, width: int) -> str:
    """Pad line to exactly `width` terminal cells on the right using spaces."""
    current_len = cell_len(line)
    if current_len > width:
        # Truncate string at exact cell width boundary
        acc = ""
        acc_len = 0
        for char in line:
            w = cell_len(char)
            if acc_len + w > width:
                break
            acc += char
            acc_len += w
        return acc + (" " * (width - acc_len))
    return line + (" " * (width - current_len))


def format_frame(raw_lines: list[str], width: int, height: int) -> str:
    """Format and normalize raw ASCII/Unicode lines into a rigid frame string."""
    lines = [_fit_and_pad_line(line, width) for line in raw_lines[:height]]
    while len(lines) < height:
        lines.append(" " * width)
    return "\n".join(lines)


def validate_frame(frame: str, expected_width: int, expected_height: int) -> None:
    """Validate that a frame has exact line count, exact cell width, and no control chars."""
    if "\t" in frame:
        raise ValueError("Frame contains tab characters")
    if "\r" in frame:
        raise ValueError("Frame contains carriage return characters")

    lines = frame.split("\n")
    if len(lines) != expected_height:
        raise ValueError(
            f"Frame height mismatch: expected {expected_height}, got {len(lines)}"
        )

    for idx, line in enumerate(lines):
        w = cell_len(line)
        if w != expected_width:
            raise ValueError(
                f"Line {idx} width mismatch: expected {expected_width} cells, got {w}"
            )


# ── FULL PROFILE FRAMES (46 cells wide x 14 rows high) ────────────────

_FULL_WIDTH: Final[int] = 46
_FULL_HEIGHT: Final[int] = 14

_FULL_IDLE_1 = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ◉      ◉  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_IDLE_2 = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ─      ─  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_LOOK_LEFT = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      | ◓       ◉  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_LOOK_CENTER = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ◉      ◉  |      |",
        r"         |      |     ▾      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_LOOK_RIGHT = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ◉       ◓ |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_ACKNOWLEDGE = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ◕      ◕  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.   < λ >      .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_THINKING_1 = format_frame(
    [
        r"              _{Σ}[01]/{Δ}\[π]_",
        r"           ./{01 10 0110 01 10}\.",
        r"         .{Σ}0110[ANTIGONA]101{Δ}.",
        r"        /01/{__1010101010__}\10\\",
        r"       /10[  |0101010101|  ]01\\",
        r"      |01   {10 10 01 01}    10|",
        r"      |10   [0]  {Δ}  [1]    01|",
        r"      |01      <  Σ  >        10|",
        r"      |10   {01__THINK__10}   01|",
        r"       \01   \1010==0101/   10/",
        r"        \10    \010110/    01/",
        r"         '{Σ}01__====__10{Δ}'",
        r"           ||  01 10 01  ||",
        r"         .-|| {codeflow} ||-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_THINKING_2 = format_frame(
    [
        r"             _[Δ]{10}<Σ>{01}[π]_",
        r"          ./10 01 1010 0101 011\.",
        r"        .{Δ}1010[COHERENCE]010{Σ}.",
        r"       /10/{__0101010101__}\01\\",
        r"      /01[  |1010101010|  ]10\\",
        r"     |10   [01 10 10 01]    01|",
        r"     |01    {1}  <π>  {0}    10|",
        r"     |10       /  Δ  \       01|",
        r"     |01   [10__PLAN__01]     10|",
        r"      \10   /0101==1010\    01/",
        r"       \01    /101001\     10/",
        r"        '{Δ}10__====__01{Σ}'",
        r"          ||  10 01 10  ||",
        r"        .-|| {thinking}  ||-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_WORKING_1 = format_frame(
    [
        r"             _[⚙]{10}<⚙>{01}[⚙]_",
        r"          ./10 01 1010 0101 011\.",
        r"        .{⚙}1010[EXECUTION]010{⚙}.",
        r"       /10/{__0101010101__}\01\\",
        r"      /01[  |⚙⚙⚙⚙⚙⚙⚙⚙⚙⚙|  ]10\\",
        r"     |10   [01 10 10 01]    01|",
        r"     |01    ⚙   <⚙>   ⚙     10|",
        r"     |10       /  ⚙  \       01|",
        r"     |01   [10__TOOL__01]     10|",
        r"      \10   /0101==1010\    01/",
        r"       \01    /101001\     10/",
        r"        '{⚙}10__====__01{⚙}'",
        r"          ||  10 01 10  ||",
        r"        .-|| {working }  ||-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_WAITING = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ◎      ◎  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \  [WAIT]  /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_SPEAKING = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  ✦    ✦  \      \\",
        r"         |      |  ✦      ✦  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \  ( ✦ )   /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_SUCCESS = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  ✓    ✓  \      \\",
        r"         |      |  ✓      ✓  |      |",
        r"         |      |     ▴      |      |",
        r"         |       \   ___    /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.  [SUCCESS]   .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)

_FULL_ERROR = format_frame(
    [
        r"             !!! [ SYSTEM ERROR ] !!!",
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

_FULL_SLEEPING = format_frame(
    [
        r"                 .-========-.",
        r"              .-'            '-.",
        r"            .'      .----.      '.",
        r"           /      .'      '.      \\",
        r"          /      /  _    _  \      \\",
        r"         |      |  ─      ─  |      |",
        r"         |      |     ·      |      |",
        r"         |       \    _     /       |",
        r"          \       '.     .'        /",
        r"           '.       '---'        .'",
        r"             '-.              .-'",
        r"               |\\          //|",
        r"             .-' \\        // '-.",
        r"          .-'     \\______//     '-.",
    ],
    _FULL_WIDTH,
    _FULL_HEIGHT,
)


# ── COMPACT PROFILE FRAMES (22 cells wide x 6 rows high) ───────────────

_COMPACT_WIDTH: Final[int] = 22
_COMPACT_HEIGHT: Final[int] = 6

_COMPACT_IDLE = format_frame(
    [
        r"    .------------.",
        r"   /  ◉        ◉  \\",
        r"  |       ▴        |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_LOOK_LEFT = format_frame(
    [
        r"    .------------.",
        r"   / ◓        ◉   \\",
        r"  |       ▴        |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_LOOK_CENTER = format_frame(
    [
        r"    .------------.",
        r"   /  ◉        ◉  \\",
        r"  |       ▾        |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_LOOK_RIGHT = format_frame(
    [
        r"    .------------.",
        r"   /   ◉        ◓  \\",
        r"  |       ▴        |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_ACKNOWLEDGE = format_frame(
    [
        r"    .------------.",
        r"   /  ◕        ◕  \\",
        r"  |     <λ>        |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_THINKING_1 = format_frame(
    [
        r"   .==[THINKING]==.",
        r"  / 01  ◎    ◎  10 \\",
        r" |  [01]  Δ  [10]   |",
        r" |     < 010 >      |",
        r"  \ 1010==010110   /",
        r"   '=============='",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_WORKING_1 = format_frame(
    [
        r"   .==[ WORKING ]==.",
        r"  / ⚙⚙  ⚙    ⚙  ⚙⚙ \\",
        r" |  [⚙]  ⚙  [⚙]   |",
        r" |     < ⚙⚙⚙ >      |",
        r"  \ 1010==010110   /",
        r"   '=============='",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_WAITING = format_frame(
    [
        r"    .------------.",
        r"   /  ◎        ◎  \\",
        r"  |     [WAIT]     |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_SPEAKING = format_frame(
    [
        r"    .------------.",
        r"   /  ✦        ✦  \\",
        r"  |      (✦)       |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_SUCCESS = format_frame(
    [
        r"    .------------.",
        r"   /  ✓        ✓  \\",
        r"  |    [OK]        |",
        r"  |     \___/      |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_ERROR = format_frame(
    [
        r"   .==[  ERROR  ]==.",
        r"  / XX  ✖    ✖  XX \\",
        r" |  [!]  X  [!]   |",
        r" |     <FAULT>      |",
        r"  \ XXXX==XXXXXX   /",
        r"   '=============='",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)

_COMPACT_SLEEPING = format_frame(
    [
        r"    .------------.",
        r"   /  ─        ─  \\",
        r"  |       ·        |",
        r"  |      ___       |",
        r"   \              /",
        r"    '------------'",
    ],
    _COMPACT_WIDTH,
    _COMPACT_HEIGHT,
)


# ── MICRO PROFILE FRAMES (5 cells wide x 1 row high) ─────────────────

_MICRO_WIDTH: Final[int] = 5
_MICRO_HEIGHT: Final[int] = 1

_MICRO_FRAMES: Final[dict[PortraitState, tuple[str, ...]]] = {
    PortraitState.IDLE: ("[◉ ◉]",),
    PortraitState.TRACKING_INPUT: ("[◉ ◉]",),
    PortraitState.LOOK_DOWN_LEFT: ("[◓ ◉]",),
    PortraitState.LOOK_DOWN_CENTER: ("[◉ ◉]",),
    PortraitState.LOOK_DOWN_RIGHT: ("[◉ ◓]",),
    PortraitState.ACKNOWLEDGE: ("[◕ ◕]",),
    PortraitState.THINKING: ("[◎ ◎]",),
    PortraitState.WORKING: ("[⚙ ⚙]",),
    PortraitState.WAITING: ("[◎ ◎]",),
    PortraitState.SPEAKING: ("[✦ ✦]",),
    PortraitState.SUCCESS: ("[✓ ✓]",),
    PortraitState.ERROR: ("[✖ ✖]",),
    PortraitState.SLEEPING: ("[- -]",),
}


# ── FRAME LIBRARIES MATRIX ────────────────────────────────────────────

_FULL_LIBRARY: Final[dict[PortraitState, tuple[str, ...]]] = {
    PortraitState.IDLE: (_FULL_IDLE_1, _FULL_IDLE_2),
    PortraitState.TRACKING_INPUT: (_FULL_IDLE_1,),
    PortraitState.LOOK_DOWN_LEFT: (_FULL_LOOK_LEFT,),
    PortraitState.LOOK_DOWN_CENTER: (_FULL_LOOK_CENTER,),
    PortraitState.LOOK_DOWN_RIGHT: (_FULL_LOOK_RIGHT,),
    PortraitState.ACKNOWLEDGE: (_FULL_ACKNOWLEDGE,),
    PortraitState.THINKING: (_FULL_THINKING_1, _FULL_THINKING_2),
    PortraitState.WORKING: (_FULL_WORKING_1, _FULL_THINKING_2),
    PortraitState.WAITING: (_FULL_WAITING,),
    PortraitState.SPEAKING: (_FULL_SPEAKING,),
    PortraitState.SUCCESS: (_FULL_SUCCESS,),
    PortraitState.ERROR: (_FULL_ERROR,),
    PortraitState.SLEEPING: (_FULL_SLEEPING,),
}

_COMPACT_LIBRARY: Final[dict[PortraitState, tuple[str, ...]]] = {
    PortraitState.IDLE: (_COMPACT_IDLE,),
    PortraitState.TRACKING_INPUT: (_COMPACT_IDLE,),
    PortraitState.LOOK_DOWN_LEFT: (_COMPACT_LOOK_LEFT,),
    PortraitState.LOOK_DOWN_CENTER: (_COMPACT_LOOK_CENTER,),
    PortraitState.LOOK_DOWN_RIGHT: (_COMPACT_LOOK_RIGHT,),
    PortraitState.ACKNOWLEDGE: (_COMPACT_ACKNOWLEDGE,),
    PortraitState.THINKING: (_COMPACT_THINKING_1,),
    PortraitState.WORKING: (_COMPACT_WORKING_1,),
    PortraitState.WAITING: (_COMPACT_WAITING,),
    PortraitState.SPEAKING: (_COMPACT_SPEAKING,),
    PortraitState.SUCCESS: (_COMPACT_SUCCESS,),
    PortraitState.ERROR: (_COMPACT_ERROR,),
    PortraitState.SLEEPING: (_COMPACT_SLEEPING,),
}

FRAME_SETS: Final[Mapping[PortraitProfile, PortraitFrameSet]] = {
    PortraitProfile.FULL: PortraitFrameSet(
        profile=PortraitProfile.FULL,
        width=_FULL_WIDTH,
        height=_FULL_HEIGHT,
        frames=tuple(
            frame for tuple_set in _FULL_LIBRARY.values() for frame in tuple_set
        ),
    ),
    PortraitProfile.COMPACT: PortraitFrameSet(
        profile=PortraitProfile.COMPACT,
        width=_COMPACT_WIDTH,
        height=_COMPACT_HEIGHT,
        frames=tuple(
            frame for tuple_set in _COMPACT_LIBRARY.values() for frame in tuple_set
        ),
    ),
    PortraitProfile.MICRO: PortraitFrameSet(
        profile=PortraitProfile.MICRO,
        width=_MICRO_WIDTH,
        height=_MICRO_HEIGHT,
        frames=tuple(
            frame for tuple_set in _MICRO_FRAMES.values() for frame in tuple_set
        ),
    ),
}


def get_portrait_frames(
    profile: PortraitProfile, state: PortraitState
) -> tuple[str, ...]:
    """Retrieve deterministic frame tuple for a given profile and PortraitState."""
    if profile == PortraitProfile.FULL:
        return _FULL_LIBRARY.get(state, (_FULL_IDLE_1,))
    if profile == PortraitProfile.COMPACT:
        return _COMPACT_LIBRARY.get(state, (_COMPACT_IDLE,))
    return _MICRO_FRAMES.get(state, ("[◉ ◉]",))


# Validate all frames during module initialization
for _profile_name, _lib, _w, _h in [
    (PortraitProfile.FULL, _FULL_LIBRARY, _FULL_WIDTH, _FULL_HEIGHT),
    (PortraitProfile.COMPACT, _COMPACT_LIBRARY, _COMPACT_WIDTH, _COMPACT_HEIGHT),
    (PortraitProfile.MICRO, _MICRO_FRAMES, _MICRO_WIDTH, _MICRO_HEIGHT),
]:
    for _st, _frames in _lib.items():
        for _fr in _frames:
            validate_frame(_fr, _w, _h)
