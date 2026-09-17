"""Regression: Up/Down arrows recall submitted input, not scroll.

Bug: the full-screen layout bound plain ``up``/``down`` to
``scroll_line_up()``/``scroll_line_down()``, so arrow keys scrolled the
transcript instead of recalling previously submitted messages / slash
commands. prompt_toolkit's built-in history navigation is a no-op for a
live-growing history in a full-screen app (``Buffer._working_lines`` is a
snapshot loaded at focus time), so AntigonaLayout keeps its own capped
in-memory recall history and routes plain ``up``/``down`` through it. Line
scrolling stays on PageUp/PageDown + Ctrl+arrows.
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from prompt_toolkit.buffer import Buffer

from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatUIState
from antigona.cli_ui.renderer import CliRenderer


@pytest.fixture
def mock_state() -> ChatUIState:
    return ChatUIState(
        messages=[],
        current_status="idle",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="test-session",
    )


@pytest.fixture
def mock_renderer() -> Mock:
    return Mock(spec=CliRenderer)


def _make_layout(mock_state, mock_renderer) -> AntigonaLayout:
    async def on_input(text: str) -> None:
        pass

    return AntigonaLayout(state=mock_state, renderer=mock_renderer, on_input=on_input)


def _keys(binding) -> tuple:
    return tuple(k.value if hasattr(k, "value") else k for k in binding.keys)


def _find(layout, keys):
    return next((b for b in layout.kb.bindings if _keys(b) == keys), None)


def _attach_buffer(layout) -> Buffer:
    buf = Buffer()
    layout.application = Mock()
    layout.application.current_buffer = buf
    return buf


def _seed(layout, items) -> None:
    layout._input_history = list(items)


# ── history recording via submit (needs an event loop) ───────────────────

@pytest.mark.asyncio
async def test_submit_records_history(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    layout._handle_submit("/status")
    await asyncio.sleep(0)
    layout._handle_submit("привет")
    await asyncio.sleep(0)
    assert layout._input_history == ["/status", "привет"]


@pytest.mark.asyncio
async def test_history_capped(mock_state, mock_renderer) -> None:
    from antigona.cli_ui.layout import _MAX_INPUT_HISTORY

    layout = _make_layout(mock_state, mock_renderer)
    for i in range(_MAX_INPUT_HISTORY + 10):
        layout._handle_submit(f"msg {i}")
        await asyncio.sleep(0)
    assert len(layout._input_history) == _MAX_INPUT_HISTORY
    assert layout._input_history[-1] == f"msg {_MAX_INPUT_HISTORY + 9}"


# ── navigation (direct, no event loop needed) ───────────────────────────

def test_up_recalls_previous_submitted_input(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    _seed(layout, ["/status", "привет"])
    buf = _attach_buffer(layout)

    layout._history_previous(Mock())

    assert buf.text == "привет"


def test_up_walks_back_and_preserves_working_line(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    _seed(layout, ["/list", "/status", "запусти задачу"])
    buf = _attach_buffer(layout)
    buf.text = "недописанный ввод"

    layout._history_previous(Mock())
    assert buf.text == "запусти задачу"
    layout._history_previous(Mock())
    assert buf.text == "/status"
    layout._history_previous(Mock())
    assert buf.text == "/list"
    # at the oldest entry, further Up does not wrap
    layout._history_previous(Mock())
    assert buf.text == "/list"


def test_down_moves_forward_and_restores_working_line(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    _seed(layout, ["/status", "привет"])
    buf = _attach_buffer(layout)
    buf.text = "недописанный ввод"

    layout._history_previous(Mock())
    assert buf.text == "привет"
    layout._history_next(Mock())
    # back at the live working line -> original unsent text restored
    assert buf.text == "недописанный ввод"


def test_empty_history_up_is_noop(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    buf = _attach_buffer(layout)
    layout._history_previous(Mock())
    assert buf.text == ""


def test_up_and_down_are_bound(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    assert _find(layout, ("up",)) is not None
    assert _find(layout, ("down",)) is not None


def test_ctrl_up_still_scrolls(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    binding = _find(layout, ("c-up",))
    assert binding is not None, "c-up must scroll"
    layout.scroll_line_up = Mock()
    binding.handler(Mock())
    layout.scroll_line_up.assert_called_once()


def test_ctrl_down_still_scrolls(mock_state, mock_renderer) -> None:
    layout = _make_layout(mock_state, mock_renderer)
    binding = _find(layout, ("c-down",))
    assert binding is not None, "c-down must scroll"
    layout.scroll_line_down = Mock()
    binding.handler(Mock())
    layout.scroll_line_down.assert_called_once()
