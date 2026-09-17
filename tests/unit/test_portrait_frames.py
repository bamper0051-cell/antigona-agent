"""Unit tests for Antigona CLI UI Portrait Frame Library (Phase L6)."""

from __future__ import annotations

from rich.cells import cell_len

from antigona.cli_ui.portrait_engine import PortraitState
from antigona.cli_ui.portrait_frames import (
    FRAME_SETS,
    PortraitProfile,
    get_portrait_frames,
    validate_frame,
)


def test_profiles_exist() -> None:
    assert PortraitProfile.FULL in FRAME_SETS
    assert PortraitProfile.COMPACT in FRAME_SETS
    assert PortraitProfile.MICRO in FRAME_SETS


def test_all_states_have_frames() -> None:
    for profile in PortraitProfile:
        for state in PortraitState:
            frames = get_portrait_frames(profile, state)
            assert len(frames) > 0, f"Profile {profile} missing frames for state {state}"


def test_frame_geometry_and_cell_width() -> None:
    expected_specs = {
        PortraitProfile.FULL: (46, 14),
        PortraitProfile.COMPACT: (22, 6),
        PortraitProfile.MICRO: (5, 1),
    }

    for profile, (exp_w, exp_h) in expected_specs.items():
        frame_set = FRAME_SETS[profile]
        assert frame_set.width == exp_w
        assert frame_set.height == exp_h

        for state in PortraitState:
            frames = get_portrait_frames(profile, state)
            for frame in frames:
                # Must not contain tabs or carriage returns
                assert "\t" not in frame
                assert "\r" not in frame

                # Strict validation helper
                validate_frame(frame, exp_w, exp_h)

                lines = frame.split("\n")
                assert len(lines) == exp_h
                for line in lines:
                    assert cell_len(line) == exp_w


def test_deterministic_lookup() -> None:
    # Repeated calls return identical tuples
    f1 = get_portrait_frames(PortraitProfile.FULL, PortraitState.THINKING)
    f2 = get_portrait_frames(PortraitProfile.FULL, PortraitState.THINKING)
    assert f1 == f2


def test_gaze_frames_distinct() -> None:
    full_left = get_portrait_frames(PortraitProfile.FULL, PortraitState.LOOK_DOWN_LEFT)
    full_center = get_portrait_frames(PortraitProfile.FULL, PortraitState.LOOK_DOWN_CENTER)
    full_right = get_portrait_frames(PortraitProfile.FULL, PortraitState.LOOK_DOWN_RIGHT)

    assert full_left != full_center
    assert full_right != full_center
    assert full_left != full_right


def test_thinking_and_working_distinct() -> None:
    thinking = get_portrait_frames(PortraitProfile.FULL, PortraitState.THINKING)
    working = get_portrait_frames(PortraitProfile.FULL, PortraitState.WORKING)
    assert thinking != working


def test_error_and_success_distinct() -> None:
    error = get_portrait_frames(PortraitProfile.FULL, PortraitState.ERROR)
    success = get_portrait_frames(PortraitProfile.FULL, PortraitState.SUCCESS)
    assert error != success


def test_micro_profile_backwards_compatibility() -> None:
    micro_idle = get_portrait_frames(PortraitProfile.MICRO, PortraitState.IDLE)
    assert micro_idle == ("[◉ ◉]",)
    assert cell_len(micro_idle[0]) == 5
