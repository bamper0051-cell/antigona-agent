"""Unit tests for the half-block portrait art renderer (cli_ui.art)."""

from __future__ import annotations

import re

from antigona.cli_ui import art


def _visible(line: str) -> int:
    """Strip ANSI truecolor sequences and measure the visible width."""
    return len(re.sub(r"\x1b\[[0-9;]*m", "", line))


def test_art_unavailable_returns_empty_when_no_asset(monkeypatch) -> None:
    """Missing asset must degrade gracefully (empty art, no exception)."""
    monkeypatch.setattr(art, "_asset_path", lambda: None)
    assert art.available() is False
    assert art.art_lines(80) == []


def test_art_renders_when_available() -> None:
    """With the real asset present, art must produce ANSI half-block lines."""
    if not art.available():
        import pytest

        pytest.skip("antigona_banner.png not present in this checkout")
    lines = art.art_lines(80)
    assert lines
    assert "\x1b[38;2;" in lines[0]
    assert "\x1b[48;2;" in lines[0]


def test_art_visible_width_within_terminal() -> None:
    """Visible width of each art row must fit the requested terminal width."""
    if not art.available():
        import pytest

        pytest.skip("antigona_banner.png not present in this checkout")
    for width in (30, 40, 60, 80, 120):
        lines = art.art_lines(width)
        if not lines:
            continue
        assert max(_visible(line) for line in lines) <= width


def test_art_breathe_does_not_break_rendering() -> None:
    """Breathing phase must not change row count or raise."""
    if not art.available():
        import pytest

        pytest.skip("antigona_banner.png not present in this checkout")
    base = art.art_lines(80, breathe=0.0)
    pulsed = art.art_lines(80, breathe=1.0)
    assert len(pulsed) == len(base)
