"""Interactive portrait feature calibration tool for Antigona CLI.

Development utility — NOT part of production CLI.

Usage:
    antigona portrait-calibrate [--profile PROFILE]

Shows the current MASTER PORTRAIT with brow and mouth markers overlaid.
Arrow keys move the selected anchor; S saves to face_map.json.

Keys:
    Tab / t       cycle between Left Brow / Right Brow / Mouth
    ←→↑↓ / hjkl  move selected anchor
    s             save current positions to face_map.json
    r             reset to current file values
    q / Ctrl-C    quit without saving
    p             cycle to next profile
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

ASSET_ROOT: Final[Path] = Path(__file__).with_name("portrait_assets")
_PROFILE_ORDER: Final[tuple[str, ...]] = ("full", "large", "medium", "compact", "mini")

# Marker symbols for calibration display
_LEFT_BROW_MARKER = "L"
_RIGHT_BROW_MARKER = "R"
_MOUTH_MARKER = "M"
_CURSOR_MARKER = "█"


def _read_lines(path: Path) -> tuple[str, ...]:
    return tuple(path.read_text(encoding="utf-8").splitlines())


def _load_face_map() -> dict[str, dict[str, Any]]:
    raw: dict[str, dict[str, Any]] = json.loads((ASSET_ROOT / "face_map.json").read_text(encoding="utf-8"))
    return raw


def _save_face_map(data: dict[str, Any]) -> None:
    (ASSET_ROOT / "face_map.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _render_calibration_frame(
    profile_name: str,
    base_lines: tuple[str, ...],
    face_map: dict[str, Any],
    left_brow: tuple[int, int],
    right_brow: tuple[int, int],
    mouth: tuple[int, int],
    selected: int,  # 0=left brow, 1=right brow, 2=mouth
) -> list[str]:
    """Render portrait with brow and mouth markers overlaid."""
    grid = [list(row) for row in base_lines]

    def set_char(x: int, y: int, ch: str) -> None:
        if 0 <= y < len(grid) and 0 <= x < len(grid[y]):
            grid[y][x] = ch

    # Draw left brow
    lx, ly = left_brow
    set_char(lx, ly, _CURSOR_MARKER if selected == 0 else _LEFT_BROW_MARKER)

    # Draw right brow
    rx, ry = right_brow
    set_char(rx, ry, _CURSOR_MARKER if selected == 1 else _RIGHT_BROW_MARKER)

    # Draw mouth
    mx, my = mouth
    set_char(mx, my, _CURSOR_MARKER if selected == 2 else _MOUTH_MARKER)

    return ["".join(row) for row in grid]


def run_calibration(profile_name: str = "full") -> None:
    """Interactive calibration session using raw terminal input."""
    import os
    import termios
    import tty

    face_map: dict[str, Any] = _load_face_map()
    profile_idx = _PROFILE_ORDER.index(profile_name) if profile_name in _PROFILE_ORDER else 0

    def get_state() -> tuple[str, dict[str, Any], tuple[int, int], tuple[int, int], tuple[int, int]]:
        pname = _PROFILE_ORDER[profile_idx % len(_PROFILE_ORDER)]
        pm = face_map[pname]
        brows = pm.get("brows", [[0, 0], [0, 0]])
        lb = (int(brows[0][0]), int(brows[0][1]))
        rb = (int(brows[1][0]), int(brows[1][1]))
        m_pos = pm.get("mouth", [0, 0])
        m = (int(m_pos[0]), int(m_pos[1]))
        return pname, pm, lb, rb, m

    selected = 0  # 0=left brow, 1=right brow, 2=mouth
    pname, pm, left_brow, right_brow, mouth = get_state()

    def draw() -> None:
        os.system("clear")
        pname_local, pm_local, lb, rb, m = get_state()
        bl = _read_lines(ASSET_ROOT / pname_local / "base.txt")
        frame = _render_calibration_frame(pname_local, bl, pm_local, left_brow, right_brow, mouth, selected)
        slot_names = {0: "LEFT BROW", 1: "RIGHT BROW", 2: "MOUTH"}
        target_name = slot_names.get(selected, "ANCHOR")
        profile = face_map[pname_local]

        print(f"\033[1;36m=== PORTRAIT CALIBRATION: {pname_local.upper()} ===\033[0m")
        print(
            f"Selected: \033[1;33m{target_name}\033[0m  "
            f"LB=({left_brow[0]},{left_brow[1]})  RB=({right_brow[0]},{right_brow[1]})  "
            f"M=({mouth[0]},{mouth[1]})"
        )
        print(
            f"Face protect: x=[{profile['face_protect'][0]},{profile['face_protect'][2]}] "
            f"y=[{profile['face_protect'][1]},{profile['face_protect'][3]}]  "
            f"Profile size: {profile['cols']}x{profile['rows']}"
        )
        print()

        # Print ruler
        cols = profile["cols"]
        ruler_tens = "".join(str(i // 10) if i % 10 == 0 else " " for i in range(cols))
        ruler_ones = "".join(str(i % 10) for i in range(cols))
        print("   " + ruler_tens)
        print("   " + ruler_ones)

        for i, line in enumerate(frame):
            is_active_row = i in {left_brow[1], right_brow[1], mouth[1]}
            row_color = "\033[1;33m" if is_active_row else "\033[0m"
            print(f"{row_color}R{i:2d} {line}\033[0m")

        print()
        print("\033[2m[Tab/t] toggle anchor  [←→↑↓/hjkl] move  [s] save  [p] next profile  [q] quit\033[0m")

    def read_key() -> str:
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "")
        return ch

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        draw()
        while True:
            key = read_key()

            if key in ("q", "\x03"):  # q or Ctrl-C
                break
            elif key in ("\t", "t"):
                selected = (selected + 1) % 3
            elif key in ("p",):
                profile_idx_local = (_PROFILE_ORDER.index(pname) + 1) % len(_PROFILE_ORDER)
                pname = _PROFILE_ORDER[profile_idx_local]
                pm = face_map[pname]
                brows = pm.get("brows", [[0, 0], [0, 0]])
                left_brow = (int(brows[0][0]), int(brows[0][1]))
                right_brow = (int(brows[1][0]), int(brows[1][1]))
                m_pos = pm.get("mouth", [0, 0])
                mouth = (int(m_pos[0]), int(m_pos[1]))

            elif key in ("up", "k"):
                if selected == 0:
                    left_brow = (left_brow[0], max(0, left_brow[1] - 1))
                elif selected == 1:
                    right_brow = (right_brow[0], max(0, right_brow[1] - 1))
                else:
                    mouth = (mouth[0], max(0, mouth[1] - 1))
            elif key in ("down", "j"):
                profile_rows = face_map[pname]["rows"]
                if selected == 0:
                    left_brow = (left_brow[0], min(profile_rows - 1, left_brow[1] + 1))
                elif selected == 1:
                    right_brow = (right_brow[0], min(profile_rows - 1, right_brow[1] + 1))
                else:
                    mouth = (mouth[0], min(profile_rows - 1, mouth[1] + 1))
            elif key in ("left", "h"):
                if selected == 0:
                    left_brow = (max(0, left_brow[0] - 1), left_brow[1])
                elif selected == 1:
                    right_brow = (max(0, right_brow[0] - 1), right_brow[1])
                else:
                    mouth = (max(0, mouth[0] - 1), mouth[1])
            elif key in ("right", "l"):
                profile_cols = face_map[pname]["cols"]
                if selected == 0:
                    left_brow = (min(profile_cols - 1, left_brow[0] + 1), left_brow[1])
                elif selected == 1:
                    right_brow = (min(profile_cols - 1, right_brow[0] + 1), right_brow[1])
                else:
                    mouth = (min(profile_cols - 1, mouth[0] + 1), mouth[1])
            elif key == "s":
                face_map[pname]["brows"] = [list(left_brow), list(right_brow)]
                face_map[pname]["mouth"] = list(mouth)
                _save_face_map(face_map)
                print("\r\n\033[1;32m✓ Saved to face_map.json\033[0m\r\n")
                import time
                time.sleep(0.8)

            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    print("\nCalibration exited.")


def run_debug(profile_name: str = "full") -> None:
    """Developer debug UI — cycles through visual states without real backend work."""
    import time

    from antigona.cli_ui.portrait import PortraitEngine

    engine = PortraitEngine()
    states = [
        ("IDLE", "idle", "center"),
        ("TYPING (far_left)", "idle", "far_left"),
        ("TYPING (right)", "idle", "right"),
        ("THINKING", "focus", "center"),
        ("WORKING", "focus", "center"),
        ("WAITING", "sad", "center"),
        ("SPEAKING", "focus", "center"),
        ("SUCCESS", "success", "center"),
        ("ERROR", "error", "center"),
    ]

    print("\033[1;33m=== PORTRAIT DEBUG MODE (SIMULATED VISUAL STATES - DEBUG ONLY) ===\033[0m")
    for name, exp, gaze in states:
        lines = engine.render(profile_name, gaze=gaze, expression=exp, phase=5)
        print(f"\n--- State: \033[1;36m{name}\033[0m ---")
        for line in lines[:8]:  # Print first 8 lines preview
            print(line)
        time.sleep(0.3)
    print("\n\033[1;32m✓ Portrait debug state cycle complete.\033[0m")


__all__ = ["run_calibration", "run_debug"]

