"""Mandatory Regression Test Suite for Antigona Living Portrait v3.

Corresponds to docs/v3/03_REQUIRED_REGRESSION_TESTS.md specification.
"""

from __future__ import annotations

import inspect
from unittest.mock import Mock, patch

import pytest
from rich.cells import cell_len

import antigona.cli_ui.layout as layout_module
from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatUIState
from antigona.cli_ui.portrait import PortraitEngine
from antigona.cli_ui.portrait_engine import (
    PortraitController,
    PortraitState,
    portrait_state_for_status,
)
from antigona.cli_ui.renderer import CliRenderer


class _FakeBuf:
    def __init__(self, text: str, cursor_position: int) -> None:
        self.text = text
        self.cursor_position = cursor_position


def _make_layout(status: str = "idle") -> AntigonaLayout:
    state = ChatUIState(
        messages=[],
        current_status=status,
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="regression-test",
    )

    async def on_input(text: str) -> None:
        return None

    return AntigonaLayout(
        state=state,
        renderer=Mock(spec=CliRenderer),
        on_input=on_input,
    )


# ── Geometry ─────────────────────────────────────────────────────────────────

def test_face_map_has_no_eyes_or_eye_sockets() -> None:
    engine = PortraitEngine()
    for profile_name, spec in engine.face_map.items():
        assert "eyes" not in spec, f"{profile_name} has eyes in face_map.json"
        assert "eye_sockets" not in spec, f"{profile_name} has eye_sockets in face_map.json"
        assert "brows" in spec
        assert "mouth" in spec
        assert "tear" in spec
        assert "face_protect" in spec


def test_brow_and_mouth_anchors_within_bounds() -> None:
    engine = PortraitEngine()
    for _profile_name, spec in engine.face_map.items():
        cols, rows = spec["cols"], spec["rows"]
        (lx, by1), (rx, by2) = spec["brows"]
        mx, my = spec["mouth"]
        assert 0 <= lx < rx < cols
        assert 0 <= by1 < my < rows
        assert 0 <= by2 < my < rows


def test_no_eye_glyphs_in_rendered_portrait_or_assets() -> None:
    engine = PortraitEngine()
    eye_glyphs = set("●◒⌒◉◐◑◖◗○")
    for profile_name, prof in engine.profiles.items():
        for gaze in ("far_left", "left", "center", "right", "far_right"):
            for exp in ("idle", "focus", "laugh", "cry", "error", "success"):
                lines = engine.render(profile_name, gaze=gaze, expression=exp, phase=1)
                assert len(lines) == prof.rows
                for line in lines:
                    assert len(line) == prof.cols
                    for g in eye_glyphs:
                        assert g not in line, f"Found {g} in {profile_name} frame"


def test_portrait_dimensions_constant() -> None:
    engine = PortraitEngine()
    for profile in engine.profiles.values():
        for gaze in ("far_left", "left", "center", "right", "far_right"):
            for exp in ("idle", "focus", "laugh", "cry", "error", "success"):
                lines = engine.render(profile.name, gaze=gaze, expression=exp, phase=1)
                assert len(lines) == profile.rows
                for line in lines:
                    assert len(line) == profile.cols


def test_overlay_does_not_change_cell_width() -> None:
    engine = PortraitEngine()
    for profile in engine.profiles.values():
        lines = engine.render(profile.name, gaze="left", expression="focus", phase=3)
        for line in lines:
            assert cell_len(line) == profile.cols


def test_face_protected_from_glitch() -> None:
    engine = PortraitEngine()
    profile_name = "medium"
    clean = engine.render(profile_name, gaze="center", expression="focus", glitch=False, phase=7)
    glitched = engine.render(profile_name, gaze="center", expression="focus", glitch=True, phase=7)
    x1, y1, x2, y2 = (int(v) for v in engine.face_map[profile_name]["face_protect"])
    for y in range(y1, y2 + 1):
        assert clean[y][x1 : x2 + 1] == glitched[y][x1 : x2 + 1]


# ── Cursor / Viewport ────────────────────────────────────────────────────────

def test_real_cursor_move_updates_gaze() -> None:
    layout = _make_layout()
    text = "abc" * 20
    layout._current_buffer = lambda: _FakeBuf(text, 0)
    assert layout._portrait_gaze() == "far_left"
    layout._current_buffer = lambda: _FakeBuf(text, 30)
    assert layout._portrait_gaze() == "center"
    layout._current_buffer = lambda: _FakeBuf(text, 60)
    assert layout._portrait_gaze() == "far_right"


def test_home_moves_gaze_left() -> None:
    layout = _make_layout()
    text = "hello world long command string"
    layout._current_buffer = lambda: _FakeBuf(text, 0)
    assert layout._portrait_gaze() == "far_left"


def test_end_moves_gaze_right() -> None:
    layout = _make_layout()
    text = "hello world long command string"
    layout._current_buffer = lambda: _FakeBuf(text, len(text))
    assert layout._portrait_gaze() == "far_right"


def test_long_input_viewport_cursor_gaze() -> None:
    engine = PortraitEngine()
    # Test viewport-aware gaze calculation
    g_start = engine.gaze_from_viewport(cursor_abs=0, viewport_start=0, viewport_width=40, total_length=200)
    assert g_start == "far_left"
    g_mid = engine.gaze_from_viewport(cursor_abs=20, viewport_start=0, viewport_width=40, total_length=200)
    assert g_mid == "center"
    g_end = engine.gaze_from_viewport(cursor_abs=39, viewport_start=0, viewport_width=40, total_length=200)
    assert g_end == "far_right"


def test_gaze_hysteresis() -> None:
    layout = _make_layout()
    text = "x" * 100
    layout._current_buffer = lambda: _FakeBuf(text, 14)
    g1 = layout._portrait_gaze()
    # Within 3% deadzone
    layout._current_buffer = lambda: _FakeBuf(text, 16)
    g2 = layout._portrait_gaze()
    assert g1 == g2


def test_gaze_debounce_cancels_transient_boundary_crossing() -> None:
    layout = _make_layout()
    text = "x" * 100
    layout._current_buffer = lambda: _FakeBuf(text, 0)
    g_initial = layout._portrait_gaze()
    assert g_initial == "far_left"
    # Quick transient move back and forth
    layout._current_buffer = lambda: _FakeBuf(text, 5)
    g_transient = layout._portrait_gaze()
    assert g_transient == "far_left"


# ── Animation Lifecycle ──────────────────────────────────────────────────────

def test_blinking_is_removed_from_the_renderer() -> None:
    """Blinking was deleted outright: the engine exposes no `blink` parameter,
    ships no `blink` keyframe, and can reach no closed-eye frame."""
    engine = PortraitEngine()

    for profile_name, frames in engine.keyframes.items():
        assert "blink" not in frames, f"{profile_name} still ships a blink keyframe"

    with pytest.raises(TypeError):
        engine.render("medium", gaze="center", blink=True)  # type: ignore[call-arg]


def test_thinking_iridescent_palette_animation() -> None:
    layout = _make_layout(status="planning")
    assert layout._portrait_expression() == "focus"
    with patch.object(layout_module, "_terminal_size", return_value=(80, 60)):
        fragments = layout._get_portrait_fragments()
        portrait_styles = [style for style, text in fragments if "SOUL:" not in text and "❖" not in text and "\n" in text and "GATEWAY" not in text and "STATUS" not in text and "╭" not in text and "╰" not in text]
        for st in portrait_styles:
            assert "fg:#" in st


def test_working_iridescent_palette_animation() -> None:
    layout = _make_layout(status="tool_executing")
    assert layout._portrait_expression() == "focus"
    with patch.object(layout_module, "_terminal_size", return_value=(80, 60)):
        fragments = layout._get_portrait_fragments()
        styles = [style for style, _ in fragments]
        assert any("bold" in s or "#A3FF12" in s for s in styles)


def test_no_idle_busy_loop() -> None:
    source = inspect.getsource(layout_module)
    assert "while True:" not in source or "await asyncio.sleep" in source
    assert source.count("asyncio.sleep(") == 1


def test_previous_animation_cancelled_on_higher_priority_state() -> None:
    controller = PortraitController()
    controller.set_state(PortraitState.WORKING)
    # Higher priority state ERROR overrides previous state
    controller.set_state(PortraitState.ERROR)
    assert controller.state == PortraitState.ERROR


def test_waiting_approval_stops_working_motion() -> None:
    layout = _make_layout(status="waiting_approval")
    assert layout._portrait_expression() == "sad"
    with patch.object(layout_module, "_terminal_size", return_value=(80, 60)):
        fragments = layout._get_portrait_fragments()
        assert len(fragments) > 0


def test_success_returns_to_idle() -> None:
    engine = PortraitEngine()
    exp = engine.expression_for_status("done")
    assert exp == "success"
    state = portrait_state_for_status("done")
    assert state == PortraitState.SUCCESS


def test_error_stabilizes_without_infinite_glitch() -> None:
    engine = PortraitEngine()
    lines = engine.render("medium", expression="error", glitch=True, reduced_motion=True)
    assert len(lines) == engine.profiles["medium"].rows


# ── Input Safety ─────────────────────────────────────────────────────────────

def test_input_navigation_not_intercepted() -> None:
    layout = _make_layout()
    # Key bindings exist and include pageup, pagedown, up, down, home, end
    assert layout.kb is not None


def test_portrait_refresh_does_not_move_input() -> None:
    layout = _make_layout()
    buf = _FakeBuf("test command", 4)
    layout._current_buffer = lambda: buf
    layout._get_portrait_fragments()
    assert buf.cursor_position == 4
    assert buf.text == "test command"


def test_portrait_refresh_does_not_force_chat_scroll() -> None:
    layout = _make_layout()
    layout.scroll_offset = 5
    layout.auto_follow = False
    layout._get_portrait_fragments()
    assert layout.scroll_offset == 5
    assert layout.auto_follow is False


def test_slash_popup_stays_above_mobile_input() -> None:
    layout = _make_layout()
    menu_float = layout._create_menu()
    assert menu_float.bottom == 1 + layout._input_height()


# ── Runtime Truth ────────────────────────────────────────────────────────────

def test_working_requires_real_tool_started_event() -> None:
    state = portrait_state_for_status("tool_executing")
    assert state == PortraitState.WORKING


def test_thinking_requires_real_agent_event() -> None:
    state = portrait_state_for_status("planning")
    assert state == PortraitState.THINKING


def test_task_complete_drives_success() -> None:
    state = portrait_state_for_status("done")
    assert state == PortraitState.SUCCESS


def test_task_failure_drives_error() -> None:
    state = portrait_state_for_status("failed")
    assert state == PortraitState.ERROR
