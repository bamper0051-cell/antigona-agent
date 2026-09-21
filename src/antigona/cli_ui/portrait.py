"""Terminal-native living portrait engine for the canonical ``antigona chat`` UI.

This module contains *presentation only*.  It knows nothing about Gateway,
workers, tools, providers, memory or task authority.  The full-screen
``AntigonaLayout`` maps already-existing ``ChatUIState`` statuses into this
renderer.

Design constraints:
- one immutable MASTER portrait per size profile;
- V2 local layers for gaze/focus;
- V1 prepared keyframes for strong expressions;
- deterministic edge-only glitch, with protected facial landmarks;
- no timers, threads, curses or stdout writes here — callers supply the phase.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from rich.cells import cell_len

ASSET_ROOT: Final[Path] = Path(__file__).with_name("portrait_assets")
_PROFILE_ORDER: Final[tuple[str, ...]] = ("full", "large", "medium", "compact", "mini")

#: The seven horizontal gaze buckets, ordered left → right.  ``GAZE_ORDER`` is
#: the canonical enumeration; every other gaze table below is keyed by it.
GAZE_ORDER: Final[tuple[str, ...]] = (
    "far_left",
    "left",
    "slight_left",
    "center",
    "slight_right",
    "right",
    "far_right",
)

#: Normalised horizontal gaze coordinate per bucket, evenly spread over
#: ``[-1.0, +1.0]``.  This is the continuous space the smoother eases through.
GAZE_X: Final[dict[str, float]] = {
    name: -1.0 + (2.0 * index) / (len(GAZE_ORDER) - 1)
    for index, name in enumerate(GAZE_ORDER)
}

#: Upper cursor-ratio bound of each bucket (the last one is the open end).
#: Symmetric around 0.5; ``left``/``right`` reach 0.32/0.68 so that a cursor at
#: exactly 30 % (pinned by the pre-existing hysteresis regression test) still
#: reads as a full ``left`` rather than tipping into ``slight_left``.
_GAZE_RATIO_BOUNDS: Final[tuple[tuple[float, str], ...]] = (
    (0.10, "far_left"),
    (0.32, "left"),
    (0.45, "slight_left"),
    (0.55, "center"),
    (0.68, "slight_right"),
    (0.90, "right"),
)

#: Time (seconds) a gaze change needs to travel ~90 % of the way to its target.
GAZE_EASE_DURATION: Final[float] = 0.15

#: Asymmetric working-FX presets: (left-half factor, right-half factor).
GLITCH_BIAS_FACTORS: Final[dict[str, tuple[float, float]]] = {
    "balanced": (1.0, 1.0),
    "left": (1.75, 0.45),
    "right": (0.45, 1.75),
}

#: Gaze bucket → glitch bias side.
GAZE_TO_BIAS: Final[dict[str, str]] = {
    "far_left": "left",
    "left": "left",
    "slight_left": "left",
    "center": "balanced",
    "slight_right": "right",
    "right": "right",
    "far_right": "right",
}
STRONG_EXPRESSIONS: Final[frozenset[str]] = frozenset(
    {"laugh", "cry", "error", "success"}
)
ACTIVE_STATUSES: Final[frozenset[str]] = frozenset(
    {"sending", "planning", "running", "tool_executing", "observing", "verifying", "reconnecting"}
)

EngineName = Literal["hybrid", "v1", "v2"]
ExpressionName = Literal["idle", "focus", "smile", "laugh", "sad", "cry", "error", "success"]


@dataclass(frozen=True)
class PortraitProfile:
    """One fixed-size terminal portrait profile."""

    name: str
    cols: int
    rows: int


def _read_lines(path: Path) -> tuple[str, ...]:
    return tuple(path.read_text(encoding="utf-8").splitlines())


def bucket_for_ratio(ratio: float) -> str:
    """Map a ``[0, 1]`` cursor ratio to one of the seven gaze buckets."""
    clamped = max(0.0, min(1.0, ratio))
    for bound, name in _GAZE_RATIO_BOUNDS:
        if clamped < bound:
            return name
    return "far_right"


def gaze_x_for(gaze: str) -> float:
    """Normalised ``[-1, +1]`` coordinate of a gaze bucket (unknown → centre)."""
    return GAZE_X.get(gaze, 0.0)


def clamp_gaze_x(value: float) -> float:
    """Clamp a raw gaze coordinate into the closed ``[-1, +1]`` range."""
    return max(-1.0, min(1.0, value))


def bucket_from_gaze_x(value: float) -> str:
    """Snap a continuous gaze coordinate onto the nearest of the seven buckets."""
    steps = len(GAZE_ORDER) - 1
    index = int(round((clamp_gaze_x(value) + 1.0) / 2.0 * steps))
    return GAZE_ORDER[max(0, min(steps, index))]


@dataclass
class GazeSmoother:
    """Eases the rendered gaze towards its target instead of snapping to it.

    The controller is time-driven but owns no timer: callers advance it with the
    elapsed ``dt`` from the loop they already run.  ``duration`` is the time a
    change needs to cover ~90 % of the distance to the target, so a gaze switch
    reads as a ~150 ms glide rather than a one-frame jump.
    """

    duration: float = GAZE_EASE_DURATION
    current_gaze_x: float = 0.0
    target_gaze_x: float = 0.0
    #: Buckets whose target must not be overwritten by cursor movement.
    locked: bool = field(default=False)

    def set_gaze_target(self, gaze: str) -> None:
        """Aim at a named bucket (no-op while locked)."""
        if self.locked:
            return
        self.target_gaze_x = clamp_gaze_x(gaze_x_for(gaze))

    def force_gaze_target(self, gaze: str) -> None:
        """Aim at a named bucket even while locked (used by the choreography)."""
        self.target_gaze_x = clamp_gaze_x(gaze_x_for(gaze))

    def update_gaze_target_from_cursor(self, position: int, text_length: int) -> str:
        """Aim at the bucket implied by an input cursor; returns that bucket."""
        gaze = PortraitEngine.gaze_from_cursor(position, text_length)
        self.set_gaze_target(gaze)
        return gaze

    def snap(self) -> None:
        """Teleport to the target (used on reset, never during a transition)."""
        self.current_gaze_x = self.target_gaze_x

    def at_target(self, tolerance: float = 0.02) -> bool:
        """True once the eased position is visually indistinguishable from target."""
        return abs(self.current_gaze_x - self.target_gaze_x) <= tolerance

    def advance(self, dt: float) -> float:
        """Ease ``current_gaze_x`` towards the target for ``dt`` seconds."""
        if dt <= 0.0:
            return self.current_gaze_x
        alpha = 1.0 - 0.1 ** (dt / max(self.duration, 1e-6))
        alpha = max(0.0, min(1.0, alpha))
        delta = self.target_gaze_x - self.current_gaze_x
        self.current_gaze_x = clamp_gaze_x(self.current_gaze_x + delta * alpha)
        if self.at_target():
            self.current_gaze_x = self.target_gaze_x
        return self.current_gaze_x

    def current_gaze_bucket(self) -> str:
        """The bucket the renderer should draw right now."""
        return bucket_from_gaze_x(self.current_gaze_x)

    def target_gaze_bucket(self) -> str:
        """The bucket the smoother is travelling towards."""
        return bucket_from_gaze_x(self.target_gaze_x)


class PortraitEngine:
    """Pure renderer over fixed portrait assets."""

    def __init__(self, asset_root: Path = ASSET_ROOT) -> None:
        self.asset_root = asset_root
        raw_map = json.loads((asset_root / "face_map.json").read_text(encoding="utf-8"))
        self.face_map: dict[str, dict[str, Any]] = raw_map
        self.base: dict[str, tuple[str, ...]] = {}
        self.keyframes: dict[str, dict[str, tuple[str, ...]]] = {}
        self.profiles: dict[str, PortraitProfile] = {}

        for name in _PROFILE_ORDER:
            spec = raw_map[name]
            self.profiles[name] = PortraitProfile(
                name=name,
                cols=int(spec["cols"]),
                rows=int(spec["rows"]),
            )
            self.base[name] = _read_lines(asset_root / name / "base.txt")
            keyframes: dict[str, tuple[str, ...]] = {}
            for path in (asset_root / name / "keyframes").glob("*.txt"):
                keyframes[path.stem] = _read_lines(path)
            self.keyframes[name] = keyframes

    def profile_for_terminal(self, cols: int, rows: int) -> PortraitProfile | None:
        """Return the largest profile that leaves usable chat history below it."""
        forced = os.getenv("ANTIGONA_PORTRAIT_SCALE", "auto").strip().lower()
        if forced in {"0", "off", "none", "false"}:
            return None
        if forced in self.profiles:
            profile = self.profiles[forced]
            if cols >= profile.cols + 2 and rows >= profile.rows + 6:
                return profile

        thresholds = (
            ("full", 84, 76),
            ("large", 70, 64),
            ("medium", 60, 52),
            ("compact", 52, 48),
            ("mini", 44, 46),
        )
        for name, min_cols, min_rows in thresholds:
            if cols >= min_cols and rows >= min_rows:
                return self.profiles[name]
        return None

    @staticmethod
    def gaze_from_viewport(
        cursor_abs: int,
        viewport_start: int = 0,
        viewport_width: int = 80,
        total_length: int = 0,
    ) -> str:
        """Map absolute cursor & viewport positions to seven horizontal gaze zones."""
        if total_length <= 0:
            return "center"
        rel_x = max(0, cursor_abs - viewport_start)
        eff_width = max(viewport_width - 1, 1)
        return bucket_for_ratio(rel_x / eff_width)

    @staticmethod
    def gaze_from_cursor(position: int, text_length: int) -> str:
        """Map input insertion point to seven horizontal gaze buckets."""
        if text_length <= 0:
            return "center"
        return bucket_for_ratio(position / max(1, text_length))

    @staticmethod
    def expression_for_status(status: str, *, mood_pulse: str | None = None) -> str:
        """Map truthful backend/UI status to a decorative facial expression."""
        if status in {"failed", "cancelled", "timeout", "error", "disconnected"}:
            return "error"
        if status == "done":
            return "success"
        if status == "waiting_approval":
            return "sad"
        if status in ACTIVE_STATUSES:
            return mood_pulse if mood_pulse in {"laugh", "cry"} else "focus"
        return "idle"

    def render(
        self,
        profile_name: str,
        *,
        gaze: str = "center",
        expression: str = "idle",
        phase: int = 0,
        engine: EngineName = "hybrid",
        glitch: bool = True,
        reduced_motion: bool = False,
        bias: str = "balanced",
        input_focus: bool = False,
        working_intensity: float = 0.0,
    ) -> tuple[str, ...]:
        """Render one fixed-geometry portrait frame.

        Note: `gaze` and `input_focus` are kept in the signature for API
        compatibility with layout callers, but eye glyph mechanics have been
        removed, so they no longer modify the facial eye sockets.

        ``bias`` biases the working glitch towards the half of the face the
        operator was last looking at; ``working_intensity`` (0..1) scales the
        glitch while a task ramps up or fades out.
        """
        if profile_name not in self.profiles:
            raise KeyError(f"Unknown portrait profile: {profile_name}")
        if gaze not in GAZE_ORDER:
            gaze = "center"
        if engine not in {"hybrid", "v1", "v2"}:
            engine = "hybrid"
        if bias not in GLITCH_BIAS_FACTORS:
            bias = "balanced"

        if engine == "v1":
            lines = self._render_v1(profile_name, gaze, expression)
        elif engine == "v2":
            lines = self._apply_layers(
                profile_name, gaze, expression, phase, input_focus
            )
        else:
            lines = self._render_hybrid(
                profile_name, gaze, expression, phase, input_focus
            )

        if not reduced_motion:
            if glitch:
                intensity_map = {
                    "focus": 0.16,
                    "laugh": 0.26,
                    "cry": 0.26,
                    "error": 0.34,
                    "idle": 0.08,
                }
                intensity = intensity_map.get(expression, 0.08)
                intensity *= 1.0 + max(0.0, min(1.0, working_intensity))
                lines = self._apply_glitch(lines, profile_name, phase, intensity, bias)

            # Add flying birds and orbiting lightning around Antigona's silhouette
            lines = self._overlay_birds_and_lightning(profile_name, lines, phase, expression)

        self._assert_geometry(profile_name, lines)
        return tuple(lines)

    def _render_v1(
        self, profile_name: str, gaze: str, expression: str
    ) -> list[str]:
        frames = self.keyframes[profile_name]
        if expression in frames:
            lines = list(frames[expression])
        else:
            lines = list(frames["idle"])
        return lines

    def _render_hybrid(
        self,
        profile_name: str,
        gaze: str,
        expression: str,
        phase: int,
        input_focus: bool = False,
    ) -> list[str]:
        if expression in STRONG_EXPRESSIONS:
            lines = list(self.keyframes[profile_name].get(expression, self.base[profile_name]))
            if expression == "cry":
                lines = self._overlay_tear(profile_name, lines, phase)
            return lines
        return self._apply_layers(
            profile_name, gaze, expression, phase, input_focus
        )

    def _apply_layers(
        self,
        profile_name: str,
        gaze: str,
        expression: str,
        phase: int,
        input_focus: bool = False,
    ) -> list[str]:
        grid = [list(row) for row in self.base[profile_name]]
        lines = ["".join(row) for row in grid]
        if expression == "cry":
            lines = self._overlay_tear(profile_name, lines, phase)
        return lines

    def _set_char_safe(self, grid: list[list[str]], x: int, y: int, char: str) -> None:
        """Set char at grid[y][x] safely within grid bounds preserving cell width."""
        if 0 <= y < len(grid):
            row = grid[y]
            if 0 <= x < len(row):
                # Ensure cell width is preserved (1:1 cell substitution)
                if cell_len(row[x]) == cell_len(char):
                    row[x] = char

    def _overlay_birds_and_lightning(
        self, profile_name: str, lines: list[str], phase: int, expression: str
    ) -> list[str]:
        """Orbit flying bird/spark symbols and flashing lightning around Antigona's silhouette."""
        grid = [list(row) for row in lines]
        spec = self.face_map[profile_name]
        x1, y1, x2, y2 = (int(v) for v in spec["face_protect"])
        cols = self.profiles[profile_name].cols
        rows = self.profiles[profile_name].rows

        # Helper to set outer ornament only outside protected face box
        def set_outer(x: int, y: int, ch: str) -> None:
            if not (x1 <= x <= x2 and y1 <= y <= y2):
                self._set_char_safe(grid, x, y, ch)

        # Bird / Spark 1 (top-left flying orbit)
        l_x = max(1, x1 - 7 + (phase * 2) % 6)
        l_y = max(1, min(rows - 2, y1 - 2 + (phase % 3)))
        bird_left = "✦" if (phase // 3) % 2 == 0 else "✧"
        set_outer(l_x, l_y, bird_left)

        # Bird / Spark 2 (top-right flying orbit)
        r_x = min(cols - 2, x2 + 4 + ((phase * 3) % 7))
        r_y = max(1, min(rows - 2, y1 - 1 + ((phase + 2) % 4)))
        bird_right = "✧" if (phase // 3) % 2 == 0 else "✦"
        set_outer(r_x, r_y, bird_right)

        # Lightning bolt 1 (left shoulder / aura)
        lt_x = max(0, x1 - 10 + ((phase * 3) % 8))
        lt_y = min(rows - 2, y1 + 4 + (phase % 4))
        bolt1 = "ϟ" if expression in {"focus", "laugh", "cry", "error"} else "☇"
        set_outer(lt_x, lt_y, bolt1)

        # Lightning bolt 2 (right shoulder / aura)
        rt_x = min(cols - 2, x2 + 6 + ((phase * 2) % 7))
        rt_y = min(rows - 2, y1 + 5 + ((phase + 1) % 4))
        bolt2 = "⌁" if (phase % 2 == 0) else "☇"
        set_outer(rt_x, rt_y, bolt2)

        return ["".join(row) for row in grid]

    def _overlay_tear(self, profile_name: str, lines: list[str], phase: int) -> list[str]:
        grid = [list(row) for row in lines]
        tx, ty = self.face_map[profile_name]["tear"]
        shift = phase % 2
        for dy, char in ((0, "╷"), (1, "│"), (2, "·")):
            self._set_char(grid, int(tx), int(ty) + dy + shift, char)
        return ["".join(row) for row in grid]

    def _apply_glitch(
        self,
        lines: list[str],
        profile_name: str,
        phase: int,
        intensity: float,
        bias: str = "balanced",
    ) -> list[str]:
        """Deterministic sparse edge glitch; canonical face box is protected.

        ``bias`` shifts the FX budget towards one half of the frame (the side
        the operator was last looking at when they hit Enter).  The protected
        face box is honoured identically for every bias.
        """
        spec = self.face_map[profile_name]
        x1, y1, x2, y2 = (int(v) for v in spec["face_protect"])
        left_factor, right_factor = GLITCH_BIAS_FACTORS.get(bias, (1.0, 1.0))
        midpoint = self.profiles[profile_name].cols / 2.0
        budget = 6.0 * intensity
        left_budget = int(budget * left_factor)
        right_budget = int(budget * right_factor)
        symbols = "01[]{}ΣΔλ/:."
        out: list[str] = []
        for y, row in enumerate(lines):
            chars = list(row)
            if y % 5 == phase % 5:
                for x, char in enumerate(chars):
                    protected = x1 <= x <= x2 and y1 <= y <= y2
                    if char != " " and not protected:
                        threshold = left_budget if x < midpoint else right_budget
                        if ((x * 17 + y * 31 + phase * 13) % 97) < threshold:
                            chars[x] = symbols[(x + y + phase) % len(symbols)]
                if not (y1 <= y <= y2):
                    shift = 1 if ((y + phase) % 2) else 2
                    shifted = (" " * shift + "".join(chars))[: len(chars)]
                    chars = list(shifted)
            out.append("".join(chars))
        return out

    def _assert_geometry(self, profile_name: str, lines: list[str] | tuple[str, ...]) -> None:
        profile = self.profiles[profile_name]
        if len(lines) != profile.rows or any(len(line) != profile.cols for line in lines):
            raise ValueError(f"Portrait geometry changed for profile {profile_name}")

    @staticmethod
    def _set_char(grid: list[list[str]], x: int, y: int, char: str) -> None:
        if 0 <= y < len(grid) and 0 <= x < len(grid[y]):
            grid[y][x] = char

    @classmethod
    def _set_text(cls, grid: list[list[str]], cx: int, y: int, text: str) -> None:
        start = cx - len(text) // 2
        for index, char in enumerate(text):
            cls._set_char(grid, start + index, y, char)


__all__ = [
    "ACTIVE_STATUSES",
    "ASSET_ROOT",
    "GAZE_EASE_DURATION",
    "GAZE_ORDER",
    "GAZE_TO_BIAS",
    "GAZE_X",
    "GLITCH_BIAS_FACTORS",
    "GazeSmoother",
    "PortraitEngine",
    "PortraitProfile",
    "bucket_for_ratio",
    "bucket_from_gaze_x",
    "clamp_gaze_x",
    "gaze_x_for",
]
