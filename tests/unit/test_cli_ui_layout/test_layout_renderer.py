"""Tests for LayoutRendererAdapter — the routing shim between ChatController and
the single full-screen layout (Rich never writes to stdout in layout mode)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from antigona.cli_ui.layout_renderer import LayoutRendererAdapter
from antigona.cli_ui.models import ChatMessageRole


@pytest.fixture
def adapter():
    return LayoutRendererAdapter()


def test_noop_before_attach(adapter, capsys):
    """Before the layout exists every call degrades to a silent no-op."""
    adapter.render_message(ChatMessageRole.USER, "hello")
    adapter.render_state(Mock())
    adapter.render_outcome(Mock())
    adapter.render_panel(Mock())
    adapter.update_panel(Mock())
    adapter.clear_terminal()
    adapter.clear_and_repaint(Mock())
    adapter.release()
    assert capsys.readouterr().out == ""  # nothing written to stdout


def test_repaint_requested_after_attach(adapter):
    """Once attached, presentation calls request a repaint on the layout."""
    layout = Mock()
    adapter.attach(layout)

    adapter.render_message(ChatMessageRole.USER, "hello")
    adapter.render_state(Mock())
    adapter.render_outcome(Mock())
    adapter.update_panel(Mock())
    adapter.clear_and_repaint(Mock())

    assert layout.request_repaint.call_count == 5


def test_clear_terminal_never_clears(adapter):
    """clear_terminal is a no-op: the layout owns the alternate screen."""
    layout = Mock()
    adapter.attach(layout)
    adapter.clear_terminal()
    layout.request_repaint.assert_not_called()


def test_live_spinner_yields_none(adapter):
    """live_spinner is a null context manager — Rich Live is never started."""
    layout = Mock()
    adapter.attach(layout)
    with adapter.live_spinner("working") as spinner:
        assert spinner is None
    layout.request_repaint.assert_not_called()


def test_release_is_safe(adapter):
    adapter.attach(Mock())
    adapter.release()  # must not raise


if __name__ == "__main__":
    pytest.main([__file__])
