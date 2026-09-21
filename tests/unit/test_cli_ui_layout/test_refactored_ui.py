"""Unit and integration tests for CLI UI refactoring (Variant A).

Validates:
1. Slash Command Selection with Arrow Keys, Tab/Right completion, Escape closing menu,
   and instant menu evaluation via _is_menu_active().
2. Output Scrolling (line, page, home, end, ctrl+up, alt+up) & Viewport math & ↓ N новых indicator.
3. Living Portrait eye anchor layer positioning & protected face landmark alignment.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

import antigona.cli_ui.layout as layout_module
from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatMessage, ChatMessageRole, ChatUIState
from antigona.cli_ui.portrait import PortraitEngine
from antigona.cli_ui.renderer import CliRenderer


class _FakeBuf:
    def __init__(self, text: str, cursor_position: int = 0) -> None:
        self.text = text
        self.cursor_position = cursor_position


def _create_test_layout() -> AntigonaLayout:
    state = ChatUIState(
        messages=[],
        current_status="idle",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="refactor-test",
    )

    async def on_input(text: str) -> None:
        return None

    layout = AntigonaLayout(
        state=state,
        renderer=Mock(spec=CliRenderer),
        on_input=on_input,
    )
    return layout


# ── Requirement 1: Slash Command Menu & Keybindings ───────────────────────────

def test_slash_menu_is_menu_active_evaluates_immediately() -> None:
    layout = _create_test_layout()
    layout._current_buffer = lambda: _FakeBuf("/th", 3)
    
    # _is_menu_active must update menu_visible synchronously without waiting for refresh loop
    assert layout.menu_visible is False
    assert layout._is_menu_active() is True
    assert layout.menu_visible is True
    assert layout._menu_prefix() == "th"


def test_slash_menu_completion_tab_and_right() -> None:
    layout = _create_test_layout()
    buf = _FakeBuf("/th", 3)
    layout._current_buffer = lambda: buf

    assert layout._is_menu_active() is True
    first_cmd = layout._menu_commands()[0]
    
    # Trigger completion
    layout._menu_complete(Mock())
    assert buf.text == first_cmd.name + " "
    assert layout.menu_visible is False


def test_slash_menu_navigation_wraps() -> None:
    layout = _create_test_layout()
    layout._current_buffer = lambda: _FakeBuf("/", 1)
    assert layout._is_menu_active() is True
    
    total = len(layout._menu_commands())
    assert total > 0
    assert layout.menu_index == 0

    layout._menu_move(-1)
    assert layout.menu_index == total - 1

    layout._menu_move(1)
    assert layout.menu_index == 0


def test_slash_menu_on_text_changed_triggers_update() -> None:
    layout = _create_test_layout()
    layout._current_buffer = lambda: _FakeBuf("/clear", 6)
    
    layout._on_input_text_changed()
    assert layout.menu_visible is True


# ── Requirement 2: Output Scrolling & Text Overflow ─────────────────────────

def test_scrolling_keybindings_and_modes() -> None:
    layout = _create_test_layout()
    
    # Add 100 messages to force overflow
    layout.state.messages = [
        ChatMessage(role=ChatMessageRole.ASSISTANT, content=f"Message line {i}")
        for i in range(100)
    ]
    with patch.object(layout_module, "_terminal_size", return_value=(80, 24)):
        layout.update_from_state()
        assert layout.max_scroll > 0
        assert layout.auto_follow is True
        assert layout.scroll_offset == 0

        # Line up
        layout.scroll_line_up()
        assert layout.auto_follow is False
        assert layout.scroll_offset == 1

        # Page up
        height = layout._center_height()
        layout.scroll_page_up()
        assert layout.scroll_offset == 1 + height

        # Jump to top
        layout.scroll_to_top()
        assert layout.scroll_offset == layout.max_scroll

        # Line down
        layout.scroll_line_down()
        assert layout.scroll_offset == layout.max_scroll - 1

        # Page down
        layout.scroll_page_down()
        assert layout.scroll_offset < layout.max_scroll - 1

        # Jump to bottom
        layout.scroll_to_bottom()
        assert layout.scroll_offset == 0
        assert layout.auto_follow is True


def test_new_messages_badge_updates_when_frozen() -> None:
    layout = _create_test_layout()
    layout.state.messages = [
        ChatMessage(role=ChatMessageRole.USER, content=f"Message {i}")
        for i in range(30)
    ]
    with patch.object(layout_module, "_terminal_size", return_value=(80, 24)):
        layout.update_from_state()
        assert layout.max_scroll > 0
        
        # Freeze viewport by scrolling up
        layout.scroll_line_up()
        assert layout.auto_follow is False

        # Add new messages while frozen
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.ASSISTANT, content="Reply 2"))
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.ASSISTANT, content="Reply 3"))
        layout.update_from_state()

        assert layout.new_message_count == 2
        assert "↓2 новых" in layout._header_extra()

        # Scroll back to bottom resets badge & resumes auto_follow
        layout.scroll_to_bottom()
        assert layout.auto_follow is True
        assert layout.new_message_count == 0


def test_multiline_text_wrapping_preserves_all_lines() -> None:
    layout = _create_test_layout()
    multiline_body = "Line A\nLine B\n" + ("LongWord " * 20)
    layout.state.messages = [ChatMessage(role=ChatMessageRole.ASSISTANT, content=multiline_body)]

    with patch.object(layout_module, "_terminal_size", return_value=(40, 20)):
        flat_lines = layout._flat_lines()
        assert len(flat_lines) >= 4  # Line A, Line B, and wrapped LongWord lines
        content = layout._get_center_content()
        assert "Line A" in content
        assert "Line B" in content


# ── Requirement 3: Living Portrait Alignment & Layers ────────────────────────

def test_portrait_eye_anchors_aligned_in_apply_layers() -> None:
    """Verifies that eyes are removed: face_map has no eyes/sockets, and no eye glyphs are rendered."""
    engine = PortraitEngine()
    eye_glyphs = set("●◒⌒◉◐◑◖◗○")

    # Test across profiles
    for profile_name in ("full", "large", "medium", "compact", "mini"):
        spec = engine.face_map[profile_name]
        assert "eyes" not in spec
        assert "eye_sockets" not in spec

        # Render with gaze=left
        rendered_left = engine.render(profile_name, gaze="left", glitch=False)
        for line in rendered_left:
            for g in eye_glyphs:
                assert g not in line

        # Blinking is REMOVED: no gaze/expression combination may ever put the
        # old closed-lid glyph "─" on an eye anchor, and the `blink` keyword
        # must no longer exist on the renderer at all.
        for gaze in ("center", "left", "right", "far_left", "far_right"):
            for expression in ("idle", "focus", "laugh", "cry", "error", "success"):
                frame = engine.render(
                    profile_name, gaze=gaze, expression=expression, glitch=False
                )
                for line in frame:
                    for g in eye_glyphs:
                        assert g not in line
        with pytest.raises(TypeError):
            engine.render(profile_name, blink=True, glitch=False)  # type: ignore[call-arg]


def test_glitch_protection_box_retains_face_landmarks() -> None:
    engine = PortraitEngine()
    profile_name = "medium"
    spec = engine.face_map[profile_name]
    x1, y1, x2, y2 = (int(v) for v in spec["face_protect"])

    frame_noglitch = engine.render(profile_name, gaze="center", glitch=False)
    frame_glitch = engine.render(profile_name, gaze="center", glitch=True, phase=42)

    # Check protected rectangle
    for y in range(y1, y2 + 1):
        assert frame_noglitch[y][x1 : x2 + 1] == frame_glitch[y][x1 : x2 + 1]
