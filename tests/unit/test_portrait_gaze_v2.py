"""Seven-way gaze, easing and asymmetric working FX for the living portrait."""

from __future__ import annotations

from antigona.cli_ui.portrait import (
    GAZE_ORDER,
    GAZE_X,
    GazeSmoother,
    PortraitEngine,
    bucket_from_gaze_x,
    clamp_gaze_x,
    gaze_x_for,
)
from antigona.cli_ui.portrait_engine import (
    PortraitController,
    portrait_mode_for_status,
    resolve_portrait_mode,
)


def _diff_cells(a: tuple[str, ...], b: tuple[str, ...]) -> list[tuple[int, int]]:
    """Coordinates where two same-geometry frames differ."""
    out: list[tuple[int, int]] = []
    for y, (row_a, row_b) in enumerate(zip(a, b, strict=True)):
        for x, (ca, cb) in enumerate(zip(row_a, row_b, strict=True)):
            if ca != cb:
                out.append((x, y))
    return out


# ── 1. Seven-way gaze ────────────────────────────────────────────────────────


def test_seven_way_gaze_covers_far_and_slight_buckets() -> None:
    engine = PortraitEngine()
    length = 100
    assert engine.gaze_from_cursor(0, length) == "far_left"
    assert engine.gaze_from_cursor(20, length) == "left"
    assert engine.gaze_from_cursor(38, length) == "slight_left"
    assert engine.gaze_from_cursor(50, length) == "center"
    assert engine.gaze_from_cursor(60, length) == "slight_right"
    assert engine.gaze_from_cursor(75, length) == "right"
    assert engine.gaze_from_cursor(100, length) == "far_right"


def test_seven_way_gaze_from_viewport_matches_cursor_buckets() -> None:
    engine = PortraitEngine()
    seen = {
        engine.gaze_from_viewport(
            cursor_abs=cursor,
            viewport_start=0,
            viewport_width=101,
            total_length=400,
        )
        for cursor in range(0, 101)
    }
    assert seen == set(GAZE_ORDER)


def test_gaze_buckets_are_monotonic_left_to_right() -> None:
    assert [gaze_x_for(name) for name in GAZE_ORDER] == sorted(GAZE_X.values())
    assert gaze_x_for("far_left") == -1.0
    assert gaze_x_for("center") == 0.0
    assert gaze_x_for("far_right") == 1.0


def test_gaze_x_is_clamped_to_unit_range() -> None:
    assert clamp_gaze_x(-7.5) == -1.0
    assert clamp_gaze_x(7.5) == 1.0
    assert bucket_from_gaze_x(-99.0) == "far_left"
    assert bucket_from_gaze_x(99.0) == "far_right"
    for name in GAZE_ORDER:
        assert bucket_from_gaze_x(gaze_x_for(name)) == name


def test_every_gaze_bucket_keeps_fixed_geometry() -> None:
    engine = PortraitEngine()
    for profile in engine.profiles.values():
        for gaze in GAZE_ORDER:
            for focus in (False, True):
                frame = engine.render(
                    profile.name, gaze=gaze, phase=11, input_focus=focus
                )
                assert len(frame) == profile.rows
                assert {len(line) for line in frame} == {profile.cols}


# ── 2. Smoothing / easing ────────────────────────────────────────────────────


def test_gaze_smoother_needs_several_steps_to_reach_target() -> None:
    smoother = GazeSmoother()
    smoother.set_gaze_target("far_right")
    assert smoother.current_gaze_x == 0.0

    first = smoother.advance(0.05)
    assert 0.0 < first < smoother.target_gaze_x, "must not snap in one step"
    second = smoother.advance(0.05)
    assert first < second < smoother.target_gaze_x, "must keep easing"
    assert not smoother.at_target()


def test_gaze_smoother_arrives_within_the_ease_duration() -> None:
    smoother = GazeSmoother()
    smoother.set_gaze_target("far_left")
    elapsed = 0.0
    while elapsed < 0.3 and not smoother.at_target():
        smoother.advance(0.05)
        elapsed += 0.05
    assert smoother.at_target()
    assert smoother.current_gaze_bucket() == "far_left"
    assert elapsed <= 0.3


def test_gaze_smoother_never_leaves_the_unit_range() -> None:
    smoother = GazeSmoother()
    for gaze in ("far_left", "far_right", "center", "far_left"):
        smoother.set_gaze_target(gaze)
        for _ in range(10):
            assert -1.0 <= smoother.advance(0.05) <= 1.0


def test_locked_smoother_ignores_cursor_targets() -> None:
    smoother = GazeSmoother()
    smoother.set_gaze_target("far_left")
    smoother.snap()
    smoother.locked = True

    smoother.update_gaze_target_from_cursor(100, 100)
    assert smoother.target_gaze_bucket() == "far_left"

    smoother.force_gaze_target("center")
    assert smoother.target_gaze_bucket() == "center"


def test_update_gaze_target_from_cursor_returns_bucket() -> None:
    smoother = GazeSmoother()
    assert smoother.update_gaze_target_from_cursor(0, 100) == "far_left"
    assert smoother.target_gaze_bucket() == "far_left"


# ── 3. State priority ────────────────────────────────────────────────────────


def test_state_priority_working_beats_everything() -> None:
    assert resolve_portrait_mode(working=True, monitoring=True, typing=True) == "working"


def test_state_priority_monitoring_beats_typing() -> None:
    assert resolve_portrait_mode(working=False, monitoring=True, typing=True) == "monitoring"


def test_state_priority_typing_beats_idle() -> None:
    assert resolve_portrait_mode(working=False, monitoring=False, typing=True) == "typing"
    assert resolve_portrait_mode(working=False, monitoring=False, typing=False) == "idle"


def test_portrait_mode_for_real_statuses() -> None:
    assert portrait_mode_for_status("tool_executing", typing=True) == "working"
    assert portrait_mode_for_status("verifying", typing=True) == "monitoring"
    assert portrait_mode_for_status("idle", typing=True) == "typing"
    assert portrait_mode_for_status("idle", typing=False) == "idle"
    assert PortraitController().resolve_mode("planning", typing=True) == "working"


# ── 4. Asymmetric working FX ─────────────────────────────────────────────────


#: The substitution alphabet ``_apply_glitch`` injects; none of it appears in
#: the braille base art, so counting it measures the FX budget directly (the
#: row shift the glitch also applies is bias-independent and cancels out).
_GLITCH_SYMBOLS = frozenset("01[]{}ΣΔλ/:.")


def _glitch_symbol_counts(frame: tuple[str, ...], cols: int) -> tuple[int, int]:
    """Glitch symbols in the left / right half of a rendered frame."""
    mid = cols / 2.0
    left = right = 0
    for row in frame:
        for x, char in enumerate(row):
            if char in _GLITCH_SYMBOLS:
                if x < mid:
                    left += 1
                else:
                    right += 1
    return left, right


def _glitch_sides(bias: str) -> tuple[int, int]:
    engine = PortraitEngine()
    name = "medium"
    frame = engine.render(name, expression="error", phase=13, glitch=True, bias=bias)
    return _glitch_symbol_counts(frame, engine.profiles[name].cols)


def test_left_bias_puts_more_working_fx_on_the_left_half() -> None:
    left, right = _glitch_sides("left")
    assert left > right


def test_right_bias_puts_more_working_fx_on_the_right_half() -> None:
    left, right = _glitch_sides("right")
    assert right > left


def test_balanced_bias_is_byte_identical_to_the_default_glitch() -> None:
    engine = PortraitEngine()
    default = engine.render("medium", expression="error", phase=13)
    balanced = engine.render("medium", expression="error", phase=13, bias="balanced")
    assert default == balanced


def test_unknown_bias_falls_back_to_balanced() -> None:
    engine = PortraitEngine()
    assert engine.render("medium", expression="error", phase=13, bias="sideways") == (
        engine.render("medium", expression="error", phase=13)
    )


def test_biased_glitch_still_protects_the_face_box() -> None:
    engine = PortraitEngine()
    name = "medium"
    clean = engine.render(name, expression="error", phase=13, glitch=False)
    x1, y1, x2, y2 = (int(v) for v in engine.face_map[name]["face_protect"])
    for bias in ("left", "right", "balanced"):
        dirty = engine.render(
            name, expression="error", phase=13, glitch=True, bias=bias, working_intensity=1.0
        )
        for y in range(y1, y2 + 1):
            assert clean[y][x1 : x2 + 1] == dirty[y][x1 : x2 + 1]


def test_working_intensity_increases_the_glitch_budget() -> None:
    engine = PortraitEngine()
    name = "medium"
    cols = engine.profiles[name].cols
    calm = engine.render(name, expression="error", phase=13, working_intensity=0.0)
    busy = engine.render(name, expression="error", phase=13, working_intensity=1.0)
    assert sum(_glitch_symbol_counts(busy, cols)) > sum(_glitch_symbol_counts(calm, cols))


# ── 5. Input-length micro-reaction ───────────────────────────────────────────


def test_input_focus_changes_only_the_two_eye_cells() -> None:
    engine = PortraitEngine()
    for profile in engine.profiles.values():
        calm = engine.render(profile.name, gaze="center", glitch=False)
        focused = engine.render(
            profile.name, gaze="center", glitch=False, input_focus=True
        )
        changed = _diff_cells(calm, focused)
        assert 1 <= len(changed) <= 2, f"{profile.name}: {changed}"


def test_input_focus_is_never_suppressed_by_a_blink_frame() -> None:
    """Blinking was removed, so nothing can override the concentrated eye glyphs:
    input_focus must always land on the two eye cells."""
    engine = PortraitEngine()
    for profile in engine.profiles.values():
        calm = engine.render(profile.name, gaze="center", glitch=False)
        focused = engine.render(
            profile.name, gaze="center", glitch=False, input_focus=True
        )
        changed = _diff_cells(calm, focused)
        assert 1 <= len(changed) <= 2, f"{profile.name}: {changed}"
        assert calm != focused
