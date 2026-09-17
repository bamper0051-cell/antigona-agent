"""Living portrait integration tests for the real ``antigona chat`` layout."""

from __future__ import annotations

import os
from unittest.mock import Mock, patch

import antigona.cli_ui.layout as layout_module
from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatUIState
from antigona.cli_ui.portrait import PortraitEngine
from antigona.cli_ui.renderer import CliRenderer


class _FakeBuf:
    def __init__(self, text: str, cursor_position: int) -> None:
        self.text = text
        self.cursor_position = cursor_position


def _layout() -> AntigonaLayout:
    state = ChatUIState(
        messages=[],
        current_status="idle",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="portrait-test",
    )

    async def on_input(text: str) -> None:
        return None

    return AntigonaLayout(
        state=state,
        renderer=Mock(spec=CliRenderer),
        on_input=on_input,
    )


def _with_status(layout: AntigonaLayout, status: str) -> AntigonaLayout:
    """Swap in a ChatUIState carrying a different panel status."""
    layout.state = ChatUIState(
        messages=[],
        current_status=status,
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="portrait-test",
    )
    return layout


def test_master_portrait_profiles_keep_fixed_geometry() -> None:
    engine = PortraitEngine()
    for profile in engine.profiles.values():
        for gaze in ("far_left", "left", "center", "right", "far_right"):
            for expression in ("idle", "focus", "laugh", "cry", "error", "success"):
                frame = engine.render(
                    profile.name,
                    gaze=gaze,
                    expression=expression,
                    phase=17,
                )
                assert len(frame) == profile.rows
                assert {len(line) for line in frame} == {profile.cols}


def test_five_way_gaze_uses_real_cursor_position() -> None:
    engine = PortraitEngine()
    text_length = 100
    assert engine.gaze_from_cursor(0, text_length) == "far_left"
    assert engine.gaze_from_cursor(20, text_length) == "left"
    assert engine.gaze_from_cursor(50, text_length) == "center"
    assert engine.gaze_from_cursor(75, text_length) == "right"
    assert engine.gaze_from_cursor(100, text_length) == "far_right"


def test_glitch_never_changes_protected_face_box() -> None:
    engine = PortraitEngine()
    name = "medium"
    clean = engine.render(
        name,
        gaze="center",
        expression="focus",
        phase=13,
        glitch=False,
    )
    glitched = engine.render(
        name,
        gaze="center",
        expression="focus",
        phase=13,
        glitch=True,
    )
    x1, y1, x2, y2 = (int(v) for v in engine.face_map[name]["face_protect"])
    for y in range(y1, y2 + 1):
        assert clean[y][x1 : x2 + 1] == glitched[y][x1 : x2 + 1]


def test_layout_contains_real_portrait_window() -> None:
    layout = _layout()
    with patch.object(layout_module, "_terminal_size", return_value=(80, 60)):
        container = layout._create_layout()
        assert container is not None
        assert layout.portrait_window is not None
        assert layout._portrait_profile() is not None
        assert layout._portrait_height() > 0
        assert layout._center_height() == 60 - 4 - layout._input_height() - layout._portrait_height()


def test_tiny_terminal_falls_back_without_breaking_input() -> None:
    layout = _layout()
    with patch.object(layout_module, "_terminal_size", return_value=(40, 28)):
        layout._create_layout()
        assert layout._portrait_profile() is None
        assert layout._portrait_height() == 0
        assert layout._center_height() == 28 - 4 - layout._input_height()
        assert layout.input_window is not None


def test_portrait_gaze_reads_prompt_toolkit_buffer_cursor() -> None:
    layout = _layout()
    text = "hello from real antigona terminal"
    layout._current_buffer = lambda: _FakeBuf(text, 0)
    assert layout._portrait_gaze() == "far_left"
    layout._current_buffer = lambda: _FakeBuf(text, len(text) // 2)
    assert layout._portrait_gaze() == "center"
    layout._current_buffer = lambda: _FakeBuf(text, len(text))
    assert layout._portrait_gaze() == "far_right"


def test_real_status_maps_to_truthful_expression() -> None:
    engine = PortraitEngine()
    assert engine.expression_for_status("planning") == "focus"
    assert engine.expression_for_status("running") == "focus"
    assert engine.expression_for_status("tool_executing") == "focus"
    assert engine.expression_for_status("waiting_approval") == "sad"
    assert engine.expression_for_status("done") == "success"
    assert engine.expression_for_status("failed") == "error"


def test_portrait_adds_no_second_refresh_loop() -> None:
    import inspect

    source = inspect.getsource(layout_module)
    assert source.count("asyncio.sleep(") == 1
    assert "call_later(" not in source
    assert "set_interval(" not in source


def test_portrait_frame_getter_is_deterministic_for_same_state() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("hello", 5)
    with patch.object(layout_module, "_terminal_size", return_value=(80, 60)):
        first = layout._get_portrait_fragments()
        second = layout._get_portrait_fragments()
        assert first == second


def test_gaze_hysteresis_prevents_jitter_at_bucket_boundary() -> None:
    """Moving the cursor slightly around a bucket boundary should NOT toggle gaze."""
    layout = _layout()
    text = "a" * 100  # 100 chars

    # Position cursor at exactly 15% (boundary between far_left/left).
    # First commit: gaze changes to 'far_left' or 'left' depending on side.
    layout._current_buffer = lambda: _FakeBuf(text, 14)
    g1 = layout._portrait_gaze()

    # Move cursor 2% further — within dead zone (8%) → gaze must NOT change.
    layout._current_buffer = lambda: _FakeBuf(text, 16)
    g2 = layout._portrait_gaze()
    assert g2 == g1, "Gaze must not change within dead zone (jitter prevention)"

    # Move cursor well past dead zone → gaze may change.
    layout._current_buffer = lambda: _FakeBuf(text, 30)
    g3 = layout._portrait_gaze()
    # At 30/100 = 30% the gaze should be 'left' (15–35%).
    assert g3 == "left"


def test_gaze_hysteresis_resets_on_empty_input() -> None:
    """Empty input must always return center gaze regardless of prior state."""
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("some text", 0)
    layout._portrait_gaze()  # commit far_left
    layout._current_buffer = lambda: _FakeBuf("", 0)
    assert layout._portrait_gaze() == "center"
    assert layout._portrait_gaze_committed == "center"


def test_eye_glow_returns_color_during_thinking() -> None:
    """Eye glow must return a non-empty color string for THINKING statuses."""
    layout = _layout()
    layout.state = layout.state.__class__(
        messages=[],
        current_status="planning",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="test",
    )
    layout._portrait_phase = 2  # mid-cycle
    color = layout._portrait_eye_color()
    assert color.startswith("#"), f"Expected hex color, got: {color!r}"
    assert len(color) == 7


def test_eye_glow_returns_empty_during_idle() -> None:
    """Eye glow must return empty string during idle state (no pulsing)."""
    layout = _layout()
    # idle state is the default
    color = layout._portrait_eye_color()
    assert color == "", f"Expected empty string during idle, got: {color!r}"


def test_face_map_eye_coords_match_idle_keyframe() -> None:
    """Eye coordinates in face_map.json must match the hand-authored idle keyframe.

    The idle keyframe was authored by placing eye glyphs at the correct eye-socket
    positions in the portrait.  The dynamic overlay must place glyphs at the same
    positions so the eyes do not appear to jump between idle keyframe and
    dynamically-rendered frames.
    """
    from pathlib import Path

    ASSET_ROOT = Path("src/antigona/cli_ui/portrait_assets")
    import json

    face_map = json.loads((ASSET_ROOT / "face_map.json").read_text())

    for profile_name in ("full", "large", "medium", "compact", "mini"):
        idle_path = ASSET_ROOT / profile_name / "keyframes" / "idle.txt"
        base_path = ASSET_ROOT / profile_name / "base.txt"
        if not idle_path.exists():
            continue

        idle_lines = idle_path.read_text().splitlines()
        base_lines = base_path.read_text().splitlines()

        # Find where idle keyframe differs from base — those are the eye positions.
        keyframe_eyes: list[tuple[int, int]] = []
        for row_i, (idle_line, base_line) in enumerate(zip(idle_lines, base_lines, strict=False)):
            if idle_line != base_line:
                for col_i, (ic, _) in enumerate(zip(idle_line, base_line, strict=False)):
                    if ic in "●◐◑○◉◒⌒":
                        keyframe_eyes.append((col_i, row_i))

        fm_eyes = [(int(e[0]), int(e[1])) for e in face_map[profile_name]["eyes"]]
        assert sorted(keyframe_eyes[:2]) == sorted(fm_eyes[:2]), (
            f"{profile_name}: face_map eyes {fm_eyes} differ from keyframe eyes {keyframe_eyes}. "
            "The dynamic eye overlay will not align with the portrait's eye sockets."
        )


# ── Living portrait v2: smoothing, Enter choreography, priority, working FX ──


def test_portrait_state_priority_prefers_real_work_over_typing() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("still typing", 4)
    assert layout._portrait_mode() == "typing"

    _with_status(layout, "tool_executing")
    assert layout._portrait_mode() == "working"

    _with_status(layout, "verifying")
    assert layout._portrait_mode() == "monitoring"

    _with_status(layout, "idle")
    layout._current_buffer = lambda: _FakeBuf("", 0)
    assert layout._portrait_mode() == "idle"


def test_layout_gaze_is_eased_instead_of_jumping() -> None:
    layout = _layout()
    text = "x" * 100
    layout._current_buffer = lambda: _FakeBuf(text, 100)

    # The *target* commits immediately (hysteresis lives upstream)...
    assert layout._portrait_gaze() == "far_right"
    # ...but the rendered bucket is still travelling.
    assert layout._portrait_gaze_rendered() == "center"

    seen: list[float] = []
    for _ in range(6):
        layout.gaze_smoother.advance(0.05)
        seen.append(layout.gaze_smoother.current_gaze_x)

    assert seen == sorted(seen), "gaze must move monotonically towards the target"
    assert len(set(seen)) > 2, "gaze must take several steps, not snap"
    assert layout._portrait_gaze_rendered() == "far_right"


def test_enter_choreography_runs_hold_then_center_then_working() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("look at the left edge", 0)
    assert layout._portrait_gaze() == "far_left"

    clock = [1_000.0]
    with patch.object(layout_module.time, "monotonic", lambda: clock[0]):
        layout.begin_submit_choreography()
        layout._portrait_last_tick = clock[0]
        # The input line is consumed on submit; real work starts right after.
        layout._current_buffer = lambda: _FakeBuf("", 0)
        _with_status(layout, "tool_executing")

        assert layout.last_gaze_before_submit == "far_left"
        assert layout._portrait_gaze_rendered() == "far_left"

        phases = [layout.portrait_current_phase]
        gaze_during_hold: list[str] = []
        for _ in range(30):
            clock[0] += 0.1
            layout._update_portrait_animation()
            if layout.portrait_current_phase == "hold":
                gaze_during_hold.append(layout._portrait_gaze_rendered())
            phases.append(layout.portrait_current_phase)

    ordered = [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]]
    assert ordered == ["hold", "return_center", "working"]
    assert set(gaze_during_hold) == {"far_left"}, "the gaze must not move while held"
    assert layout._portrait_gaze_rendered() == "center"
    assert layout.working_intensity > 0.0


def test_working_locks_the_gaze_to_center() -> None:
    layout = _layout()
    _with_status(layout, "tool_executing")
    layout._current_buffer = lambda: _FakeBuf("y" * 50, 50)
    assert layout._portrait_gaze() == "center"
    assert layout.gaze_smoother.target_gaze_bucket() == "center"


def test_second_submit_invalidates_the_previous_choreography() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("first line", 0)
    layout._portrait_gaze()
    first = layout.begin_submit_choreography()
    assert layout.last_gaze_before_submit == "far_left"

    # While holding, the cursor is ignored — the held gaze is the truth.
    layout._current_buffer = lambda: _FakeBuf("second line", 11)
    assert layout._portrait_gaze() == "far_left"

    # Once the previous chain is released, the next submit captures the new gaze.
    layout.cancel_submit_choreography()
    layout._portrait_gaze()
    second = layout.begin_submit_choreography()

    assert second > first + 1, "every cancel/submit must bump the generation"
    assert layout.portrait_current_phase == "hold"
    assert layout.last_gaze_before_submit == "far_right"


def test_new_submit_clears_the_previous_working_intensity() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("first line", 0)
    layout._portrait_gaze()
    layout.working_intensity = 1.0

    layout.begin_submit_choreography()

    # A fresh submit must not carry working FX into hold/return_center.
    assert layout.working_intensity == 0.0
    assert layout.portrait_current_phase == "hold"
    assert layout._portrait_glitch_bias() == "balanced"


def test_working_fx_bias_follows_the_gaze_at_submit_time() -> None:
    layout = _layout()
    layout.working_intensity = 0.5

    layout.last_gaze_before_submit = "far_left"
    assert layout._portrait_glitch_bias() == "left"
    layout.last_gaze_before_submit = "slight_right"
    assert layout._portrait_glitch_bias() == "right"
    layout.last_gaze_before_submit = "center"
    assert layout._portrait_glitch_bias() == "balanced"

    # No working FX in flight → never biased.
    layout.working_intensity = 0.0
    layout.last_gaze_before_submit = "far_left"
    assert layout._portrait_glitch_bias() == "balanced"


def test_long_input_triggers_the_concentrated_eyes() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("short line", 3)
    assert layout._portrait_input_focus() is False

    layout._current_buffer = lambda: _FakeBuf("a" * 40, 3)
    assert layout._portrait_input_focus() is True

    # Real work outranks the typing micro-reaction.
    _with_status(layout, "tool_executing")
    assert layout._portrait_input_focus() is False


def test_portrait_debug_line_is_opt_in() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("debug me", 2)
    with patch.object(layout_module, "_terminal_size", return_value=(80, 60)):
        plain_height = layout._portrait_height()
        plain = "".join(text for _style, text in layout._get_portrait_fragments())
        assert "working_intensity=" not in plain

        with patch.dict(os.environ, {"ANTIGONA_PORTRAIT_DEBUG": "1"}):
            assert layout._portrait_height() == plain_height + 1
            debugged = "".join(text for _style, text in layout._get_portrait_fragments())

    assert "working_intensity=" in debugged
    assert "last_gaze_before_submit=" in debugged
    assert "current_phase=" in debugged


def test_cancelled_choreography_leaves_no_residual_effects() -> None:
    layout = _layout()
    layout._current_buffer = lambda: _FakeBuf("left edge please", 0)
    layout._portrait_gaze()
    layout.begin_submit_choreography()
    layout.portrait_current_phase = "working"
    layout.working_intensity = 1.0

    layout.cancel_submit_choreography()
    layout.gaze_smoother.snap()

    assert layout.portrait_current_phase is None
    assert layout.gaze_smoother.locked is False
    # The cancel itself must drop the working FX — no residual bias/intensity.
    assert layout.working_intensity == 0.0
    assert layout._portrait_glitch_bias() == "balanced"

    pristine = _layout()
    pristine._current_buffer = layout._current_buffer
    pristine._portrait_gaze()
    pristine.gaze_smoother.snap()
    with patch.object(layout_module, "_terminal_size", return_value=(80, 60)):
        assert layout._get_portrait_fragments() == pristine._get_portrait_fragments()
