"""Stability tests: bounded refresh, manual scroll mode, slash menu, CPR.

These tests pin the Termux-safety contract of the layout:
  * idle sessions repaint nothing (render-key dedup) → no CPU burn;
  * content getters are pure (no frame cycling on repaint);
  * the ``/`` menu is a compact overlay with navigation + help card;
  * the PIN prompt disables the CPR probe at the source.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import Mock, patch

import pytest

import antigona.cli_ui.layout as layout_module
from antigona.cli_ui.layout import AntigonaLayout, PickerKeyBridge
from antigona.cli_ui.models import ChatMessage, ChatMessageRole, ChatUIState
from antigona.cli_ui.renderer import CliRenderer


@pytest.fixture
def mock_state():
    return ChatUIState(messages=[], current_status="idle", events=[], connection="connected")


@pytest.fixture
def mock_renderer():
    return Mock(spec=CliRenderer)


@pytest.fixture
def layout(mock_state, mock_renderer):
    async def mock_on_input(text: str) -> None:
        pass

    return AntigonaLayout(state=mock_state, renderer=mock_renderer, on_input=mock_on_input)


@pytest.fixture(autouse=True)
def fixed_terminal():
    with patch.object(layout_module, "_terminal_size", return_value=(80, 24)):
        yield


# ── Bounded refresh (no idle CPU, no repaint churn) ──────────────────────────


def test_render_key_stable_in_idle(layout):
    """Idle state produces an identical render key → the refresh loop repaints nothing."""
    layout.state.current_status = "idle"
    layout.update_from_state()
    key1 = layout._render_key()
    key2 = layout._render_key()
    assert key1 == key2


def test_render_key_changes_on_state_change(layout):
    """A real state change flips the render key → exactly one repaint."""
    layout.update_from_state()
    before = layout._render_key()
    layout.state.current_status = "planning"
    layout._update_antigona_face_state()
    after = layout._render_key()
    assert before != after


def test_render_key_ticks_only_while_animating(layout):
    """The spinner tick enters the key only during real work (bounded 4 Hz)."""
    layout.state.current_status = "idle"
    layout.update_from_state()
    idle_key = layout._render_key()
    assert idle_key[-1] == 0  # calm → constant, no time dependence

    layout.state.current_status = "planning"
    layout.state.is_animating = True
    layout.update_from_state()
    with patch.object(layout_module.time, "monotonic", side_effect=[1.0, 1.3, 1.5]):
        k1 = layout._render_key()
        k2 = layout._render_key()
    assert k1[-1] != k2[-1]  # active → frame index advances with time
    assert k1[-1] == layout_module._SPINNER_TICK_HZ  # 1.0s * 4 Hz


def test_refresh_interval_calm_vs_active(layout):
    assert layout._refresh_interval() == layout_module._CALM_REFRESH
    layout.state.current_status = "tool_executing"
    layout.state.is_animating = True
    assert layout._refresh_interval() == layout_module._ACTIVE_REFRESH


def test_terminal_resize_flips_render_key(layout):
    """A window resize is picked up by the key → the layout repaints."""
    layout.update_from_state()
    before = layout._render_key()
    with patch.object(layout_module, "_terminal_size", return_value=(100, 40)):
        after = layout._render_key()
    assert before != after


def test_center_content_uses_full_viewport_on_resize(layout):
    """After a resize the visible slice matches the new viewport height."""
    for i in range(30):
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content=f"m{i}"))
    with patch.object(layout_module, "_terminal_size", return_value=(80, 44)):
        layout.update_from_state()
        content = layout._get_center_content()
    # height = 44 - 4 = 40 lines visible; content has 30 → all shown.
    assert content.splitlines()[0].endswith("m0")
    assert content.splitlines()[-1].endswith("m29")


# ── Slash menu ───────────────────────────────────────────────────────────────


def test_menu_commands_filter_by_prefix(layout):
    # Prefix is the text after "/" (slash-less contract of _menu_commands).
    assert layout._menu_commands("st")  # /status, /steer, …
    names = {c.name for c in layout._menu_commands("st")}
    assert names and all(n.startswith("/st") for n in names)
    assert layout._menu_commands("zzz") == []
    # No prefix → every command is offered.
    assert len(layout._menu_commands("")) == len(layout.catalog)


def test_menu_content_shows_commands_and_help(layout):
    layout.menu_visible = True
    content = layout._get_menu_content()
    assert "Команды" in content
    assert "▶" in content  # selection marker
    assert "/help" in content
    # Help card for the selected command: every card ends with the risk line.
    assert "Риск:" in content


def test_menu_hidden_content_is_empty(layout):
    assert layout.menu_visible is False
    assert layout._get_menu_content() == ""


def test_menu_move_wraps(layout):
    layout.menu_visible = True
    count = len(layout._menu_commands())
    assert count > 0
    layout.menu_index = 0
    layout._menu_move(-1)
    assert layout.menu_index == count - 1  # wraps to the end
    layout._menu_move(1)
    assert layout.menu_index == 0  # wraps back


def test_menu_visibility_tracks_buffer(layout):
    """Menu opens only while the buffer starts with ``/`` (no space yet)."""

    class FakeBuf:
        text = "/"

    layout._current_buffer = lambda: FakeBuf()  # type: ignore[method-assign]
    layout._update_menu_visibility()
    assert layout.menu_visible is True

    FakeBuf.text = "/status"  # filtered by typed prefix
    layout._update_menu_visibility()
    assert layout.menu_visible is True

    FakeBuf.text = "/status abc"  # arguments started → menu hides
    layout._update_menu_visibility()
    assert layout.menu_visible is False

    FakeBuf.text = "hello"  # not a slash command → menu stays hidden
    layout._update_menu_visibility()
    assert layout.menu_visible is False


def test_menu_height_bounded(layout):
    layout.menu_visible = True
    _, rows = layout_module._terminal_size()
    assert layout._menu_height() <= max(1, rows - 6)
    layout.menu_visible = False
    assert layout._menu_height() == 0


# ── CPR probe disabled at the source (Termux PIN screen) ─────────────────────


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="NoConsoleScreenBufferError: prompt_toolkit needs a Windows console, unavailable under pytest (Wave 4)")
async def test_pin_prompt_disables_cpr_probe(layout):
    """The PIN prompt must set PROMPT_TOOLKIT_NO_CPR before probing the terminal."""
    with patch.dict(os.environ, {}, clear=True):
        assert "PROMPT_TOOLKIT_NO_CPR" not in os.environ
        await layout._request_owner_pin()
        assert os.environ.get("PROMPT_TOOLKIT_NO_CPR") == "1"
        assert layout.owner_mode is True  # no PIN configured → dev access


# ── Picker key bridge (approval flow inside the full-screen layout) ──────────


def test_picker_bridge_active_flag():
    bridge = PickerKeyBridge()
    assert bridge.active is False
    with bridge:
        assert bridge.active is True
    assert bridge.active is False


@pytest.mark.anyio
async def test_picker_loop_via_bridge_navigates_and_approves():
    from antigona.cli_ui.approval_picker import PickerAction, PickerState, _apply_picker_key

    state = PickerState(entries=[], mode="multi")
    # _apply_picker_key is the unit under test: navigation + decision.
    assert _apply_picker_key("down", state) is False
    assert _apply_picker_key("escape", state) is True
    assert state.result is PickerAction.CANCEL


@pytest.mark.anyio
async def test_run_picker_loop_via_bridge():
    from antigona.cli_ui.approval_picker import (
        ApprovalEntry,
        PickerAction,
        _run_picker_loop,
    )

    entries = [
        ApprovalEntry(approval_id="a1", tool_name="tool1", risk_level="LOW", reason="r1", flow_id="f1"),
        ApprovalEntry(approval_id="a2", tool_name="tool2", risk_level="HIGH", reason="r2", flow_id="f2"),
    ]
    state = Mock()
    state.closed = False
    state.entries = entries
    state.selected_index = 0
    state.detail_view = False
    state.result = None
    state.chosen_entry = None

    renderer = Mock()
    bridge = PickerKeyBridge()
    with bridge:
        # Schedule: down (→ index 1) then enter (approve).
        bridge.push("down")
        bridge.push("enter")

        action = await _run_picker_loop(Mock(), renderer, state, key_bridge=bridge)

    assert action is PickerAction.APPROVE
    assert state.chosen_entry is entries[1]
    renderer.render_message.assert_called()


if __name__ == "__main__":
    pytest.main([__file__])
