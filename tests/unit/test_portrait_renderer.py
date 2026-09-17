"""Unit tests for Responsive Portrait Renderer & Profile Selection (Phase L7)."""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from antigona.cli_ui.portrait_engine import PortraitState
from antigona.cli_ui.portrait_frames import PortraitProfile
from antigona.cli_ui.portrait_renderer import (
    PortraitRenderer,
    choose_portrait_profile,
)


@pytest.mark.parametrize(
    ("cols", "lines", "expected_profile"),
    [
        (140, 50, PortraitProfile.FULL),
        (120, 40, PortraitProfile.FULL),
        (100, 30, PortraitProfile.FULL),
        (80, 24, PortraitProfile.COMPACT),
        (70, 22, PortraitProfile.COMPACT),
        (60, 20, PortraitProfile.COMPACT),
        (50, 18, PortraitProfile.MICRO),
        (40, 16, PortraitProfile.MICRO),
        (30, 12, PortraitProfile.MICRO),
    ],
)
def test_size_matrix_profile_selection(
    cols: int, lines: int, expected_profile: PortraitProfile
) -> None:
    assert choose_portrait_profile(cols, lines) == expected_profile


def test_height_aware_restriction() -> None:
    # Wide terminal but very short height -> cannot fit FULL (14 lines)
    assert choose_portrait_profile(140, 20) == PortraitProfile.COMPACT
    assert choose_portrait_profile(140, 15) == PortraitProfile.MICRO


def test_width_aware_restriction() -> None:
    # High terminal but narrow width -> cannot fit FULL (46 cols) or COMPACT (22 cols with margin)
    assert choose_portrait_profile(50, 50) == PortraitProfile.MICRO


def test_renderer_output_dimensions() -> None:
    renderer = PortraitRenderer()

    # FULL profile
    full_frame = renderer.render(PortraitProfile.FULL, PortraitState.IDLE)
    full_lines = full_frame.split("\n")
    assert len(full_lines) == 14
    for line in full_lines:
        assert cell_len(line) == 46

    # COMPACT profile
    compact_frame = renderer.render(PortraitProfile.COMPACT, PortraitState.IDLE)
    compact_lines = compact_frame.split("\n")
    assert len(compact_lines) == 6
    for line in compact_lines:
        assert cell_len(line) == 22

    # MICRO profile
    micro_frame = renderer.render(PortraitProfile.MICRO, PortraitState.IDLE)
    assert cell_len(micro_frame) == 5


def test_renderer_state_mapping_preserved() -> None:
    renderer = PortraitRenderer()
    for profile in PortraitProfile:
        for state in PortraitState:
            rendered = renderer.render(profile, state)
            assert rendered is not None
            assert len(rendered) > 0


def test_renderer_is_pure_no_side_effects() -> None:
    renderer = PortraitRenderer()
    frame1 = renderer.render(PortraitProfile.FULL, PortraitState.THINKING, frame_index=0)
    frame2 = renderer.render(PortraitProfile.FULL, PortraitState.THINKING, frame_index=0)
    assert frame1 == frame2
