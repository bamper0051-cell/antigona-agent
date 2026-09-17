"""Tests for the Antigona CLI layout with new features."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

import antigona.cli_ui.layout as layout_module
from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatMessage, ChatMessageRole, ChatUIState
from antigona.cli_ui.renderer import CliRenderer


@pytest.fixture
def mock_state():
    """Create a mock ChatUIState for testing."""
    return ChatUIState(
        messages=[],
        current_status="idle",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="test-session"
    )


@pytest.fixture
def mock_renderer():
    """Create a mock CliRenderer for testing."""
    return Mock(spec=CliRenderer)


@pytest.fixture
def layout(mock_state, mock_renderer):
    """Create an AntigonaLayout instance for testing."""
    async def mock_on_input(text: str) -> None:
        pass

    return AntigonaLayout(
        state=mock_state,
        renderer=mock_renderer,
        on_input=mock_on_input
    )


@pytest.fixture(autouse=True)
def fixed_terminal():
    """Deterministic terminal geometry: 80x24 → content width 77, center 20."""
    with patch.object(layout_module, "_terminal_size", return_value=(80, 24)):
        yield


def test_layout_initialization(layout, mock_state, mock_renderer):
    """Test that the layout initializes correctly."""
    assert layout.state == mock_state
    assert layout.renderer == mock_renderer
    assert layout.scroll_offset == 0
    assert layout.auto_follow is True
    assert layout.max_scroll == 0
    assert layout.new_message_count == 0
    assert layout.new_events_indicator == 0
    assert layout.last_event_count == 0
    assert layout.current_face_state == "IDLE"
    assert layout.owner_mode is False
    assert layout.pin_attempts == 0
    assert layout.max_pin_attempts == 3
    assert layout.menu_visible is False


def test_update_antigona_face_state_idle(layout):
    """Test face state update for idle status."""
    layout.state.current_status = "idle"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "IDLE"


def test_update_antigona_face_state_planning(layout):
    """Test face state update for planning status."""
    layout.state.current_status = "planning"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "PLANNING"


def test_update_antigona_face_state_tool_executing(layout):
    """Test face state update for tool executing status."""
    layout.state.current_status = "tool_executing"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "RUNNING_TOOL"


def test_update_antigona_face_state_verifying(layout):
    """Test face state update for verifying status."""
    layout.state.current_status = "verifying"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "VERIFYING"


def test_update_antigona_face_state_done(layout):
    """Test face state update for done status."""
    layout.state.current_status = "done"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "SUCCESS"


def test_update_antigona_face_state_failed(layout):
    """Test face state update for failed status."""
    layout.state.current_status = "failed"
    layout._update_antigona_face_state()
    assert layout.current_face_state == "ERROR"


def test_face_is_static_pure_getter(layout):
    """The face getter must be pure: repeated calls never mutate state."""
    layout.state.current_status = "planning"
    layout._update_antigona_face_state()
    first = layout._get_antigona_face()
    second = layout._get_antigona_face()
    assert first == second  # No frame cycling on repeated renders
    assert first.strip() == "[◎ ◎]"


def test_face_fixed_width_across_states(layout):
    """The face occupies a fixed cell width regardless of the state glyph."""
    widths = set()
    for status, _state_name in layout_module._STATUS_TO_FACE.items():
        layout.state.current_status = status
        layout._update_antigona_face_state()
        widths.add(layout_module.cell_len(layout._get_antigona_face()))
    assert widths == {layout_module._FACE_WIDTH_CELLS}


class _FakeBuf:
    """Minimal stand-in for prompt_toolkit's Buffer (text + cursor_position)."""

    def __init__(self, text: str, cursor_position: int | None = None) -> None:
        self.text = text
        self.cursor_position = len(text) if cursor_position is None else cursor_position


def test_gaze_center_when_buffer_empty(layout):
    """No input yet: eyes default to the center/down glyph, not left or right."""
    layout._current_buffer = lambda: _FakeBuf("")
    assert layout._get_gaze_glyph().strip() == layout_module._GAZE_CENTER


def test_gaze_center_when_no_buffer_available(layout):
    """Before the Application exists (_current_buffer() -> None) gaze is centered."""
    assert layout.application is None
    assert layout._get_gaze_glyph().strip() == layout_module._GAZE_CENTER


def test_gaze_left_when_cursor_near_start(layout):
    """Cursor in the first third of the typed text -> looking left."""
    layout._current_buffer = lambda: _FakeBuf("hello world", cursor_position=1)
    assert layout._get_gaze_glyph().strip() == layout_module._GAZE_LEFT


def test_gaze_right_when_cursor_near_end(layout):
    """Cursor in the last third of the typed text -> looking right."""
    text = "hello world"
    layout._current_buffer = lambda: _FakeBuf(text, cursor_position=len(text))
    assert layout._get_gaze_glyph().strip() == layout_module._GAZE_RIGHT


def test_gaze_center_when_cursor_in_middle(layout):
    """Cursor around the middle third -> centered/looking down at input."""
    text = "hello world"
    layout._current_buffer = lambda: _FakeBuf(text, cursor_position=len(text) // 2)
    assert layout._get_gaze_glyph().strip() == layout_module._GAZE_CENTER


def test_gaze_fixed_width_across_directions(layout):
    """Every gaze variant occupies exactly the same cell width (no header resize)."""
    samples = [
        _FakeBuf(""),
        _FakeBuf("hi", cursor_position=0),
        _FakeBuf("hi", cursor_position=1),
        _FakeBuf("hello world", cursor_position=1),
        _FakeBuf("hello world", cursor_position=6),
        _FakeBuf("hello world", cursor_position=11),
    ]
    widths = set()
    for buf in samples:
        layout._current_buffer = lambda buf=buf: buf
        widths.add(layout_module.cell_len(layout._get_gaze_glyph()))
    assert widths == {layout_module._GAZE_WIDTH_CELLS}


def test_gaze_is_pure_getter(layout):
    """Repeated calls with the same buffer state must return the same glyph."""
    layout._current_buffer = lambda: _FakeBuf("hello", cursor_position=0)
    first = layout._get_gaze_glyph()
    second = layout._get_gaze_glyph()
    assert first == second


def test_header_content_unchanged_height_across_gaze_directions(layout):
    """The header must stay a fixed 2-line block regardless of gaze direction."""
    layout.header_window = layout._create_header()
    get_header_content = layout.header_window.content.text  # FormattedTextControl callable

    for buf in (
        _FakeBuf(""),
        _FakeBuf("hello world", cursor_position=1),
        _FakeBuf("hello world", cursor_position=6),
        _FakeBuf("hello world", cursor_position=11),
    ):
        layout._current_buffer = lambda buf=buf: buf
        fragments = get_header_content()
        text = "".join(t for _style, t in fragments)
        assert text.count("\n") == 2  # exactly 3 lines (cybernetic mini-monitoring header)


def test_gaze_no_new_animation_timer_introduced():
    """Guard against reintroducing a per-frame timer/tick for the face/gaze.

    Both prior attempts at an animated face were reverted because a timer
    driving frame changes caused render churn / CPR noise (see
    docs/CLI_DEVELOPMENT.md and commits 631de1f5 / 455a9189).
    The gaze feature must stay purely event-driven: no new ``asyncio.sleep``,
    ``call_later``/``call_soon`` loop, or frame-index counter.
    """
    import inspect

    source = inspect.getsource(layout_module)

    # Exactly one bounded sleep call, inside the pre-existing _refresh_loop.
    assert source.count("asyncio.sleep(") == 1
    refresh_loop_source = inspect.getsource(layout_module.AntigonaLayout._refresh_loop)
    assert "asyncio.sleep(" in refresh_loop_source

    # No prompt_toolkit event-loop callback-scheduling primitives anywhere.
    assert "call_later(" not in source
    assert "call_soon(" not in source
    assert "set_interval(" not in source

    # The gaze getter itself must not reference asyncio/time at all — it is a
    # pure read of the already-current cursor position, not a self-driven tick.
    gaze_source = inspect.getsource(layout_module.AntigonaLayout._get_gaze_glyph)
    assert "asyncio" not in gaze_source
    assert "time." not in gaze_source


def test_color_introduces_no_new_animation_timer():
    """The AURORA colour pass must not add a second timer/tick either.

    Same guard as ``test_gaze_no_new_animation_timer_introduced``, scoped to
    the header/center colour getters added for the "переливается цветами"
    palette pass: they read already-current state (status/connection/role),
    never a frame index or a clock, so a repaint they ride on is always one
    that was going to happen anyway (state change, keystroke, or the
    pre-existing gated spinner tick) — never a colour-only tick.
    """
    import inspect

    # Still exactly one bounded sleep call in the whole module (the
    # pre-existing _refresh_loop) — the colour pass added zero new loops.
    source = inspect.getsource(layout_module)
    assert source.count("asyncio.sleep(") == 1
    assert "call_later(" not in source
    assert "call_soon(" not in source
    assert "set_interval(" not in source

    for getter_name in (
        "_get_center_fragments",
        "_header_extra_color",
    ):
        getter_source = inspect.getsource(getattr(layout_module.AntigonaLayout, getter_name))
        assert "asyncio" not in getter_source
        assert "time." not in getter_source

    # The header's inner content getter is a closure, not a bound method —
    # pull it from a live instance instead of the class.
    header_closure_source = inspect.getsource(layout_module.AntigonaLayout._create_header)
    assert "asyncio.sleep" not in header_closure_source
    assert "time.monotonic" not in header_closure_source


def test_header_fragments_are_valid_style_text_pairs(layout):
    """Header content is a list of (style, text) pairs prompt_toolkit can render.

    Regression guard for the switch from a plain string to formatted text:
    every fragment must be a 2-tuple of strings, and any non-empty style must
    be a real prompt_toolkit style string (``fg:#RRGGBB`` / ``bold``), never
    raw markup or an ANSI escape sequence smuggled into the text.
    """
    layout.state.current_status = "tool_executing"
    layout.state.connection = "reconnecting"
    fragments = layout._create_header().content.text()
    assert isinstance(fragments, list)
    assert fragments  # never empty
    for item in fragments:
        assert isinstance(item, tuple)
        assert len(item) == 2
        style, text = item
        assert isinstance(style, str)
        assert isinstance(text, str)
        assert "\x1b" not in text
        if style:
            assert style.startswith("fg:#") or style.strip() == "bold" or "bold" in style


def test_header_wordmark_uses_static_letter_gradient(layout):
    """The ANTIGONA wordmark is a static per-letter gradient (panel.BANNER_COLORS).

    Not an animation: the same 8 colours in the same order every call, with
    no dependency on time or a frame counter (option (b) from the palette
    brief — a manually computed static gradient, not a shimmer effect).
    """
    fragments_1 = layout._create_header().content.text()
    fragments_2 = layout._create_header().content.text()
    letters_1 = [(s, t) for s, t in fragments_1 if t in "ANTIGONA" and len(t) == 1]
    letters_2 = [(s, t) for s, t in fragments_2 if t in "ANTIGONA" and len(t) == 1]
    assert letters_1 == letters_2  # deterministic, no time-based drift
    assert "".join(t for _s, t in letters_1) == "ANTIGONA"
    # 8 colours pulled straight from panel.BANNER_COLORS, through the same
    # contrast guard the header itself applies (_wordmark_color swaps the
    # one stop that equals the header's own background for white).
    assert [s for s, _t in letters_1] == [
        f"fg:{layout_module._wordmark_color(color)}"
        for _letter, color in layout_module.BANNER_COLORS
    ]


def test_header_wordmark_never_matches_header_background(layout):
    """No wordmark letter renders in the same colour as the header's own bg.

    Regression test for a real bug caught while writing this palette pass:
    ``panel.BANNER_COLORS`` includes VIOLET as a gradient stop (the letter
    "I"), and the header fills its own background with that same VIOLET —
    rendered naively, that letter is invisible (fg == bg). Verified by
    rendering the header into a real prompt_toolkit ``Screen`` and reading
    back each cell's resolved (fg, bg) ``Attrs``, not just the fragment
    strings, so the *actual* composited colours are what's asserted.
    """
    from prompt_toolkit.layout.containers import WritePosition
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen
    from prompt_toolkit.styles import Style

    win = layout._create_header()
    screen = Screen()
    screen.show_cursor = False
    win._write_to_screen_at_index(
        screen,
        MouseHandlers(),
        WritePosition(xpos=0, ypos=0, width=80, height=2),
        parent_style="",
        erase_bg=False,
    )
    resolver = Style.from_dict({})
    found_wordmark = False
    for x in range(0, 80):
        cell = screen.data_buffer[0][x]
        if cell.char not in "ANTIGONA" or not cell.char.strip():
            continue
        found_wordmark = True
        attrs = resolver.get_attrs_for_style_str(cell.style)
        assert attrs.color != attrs.bgcolor, f"letter {cell.char!r} at col {x} is invisible"
    assert found_wordmark


def test_header_status_color_matches_panel_state_meta(layout):
    """The header's status word reuses panel.STATE_META's colour slot.

    Guards the "one palette, not three" design: the same status must render
    in the same accent colour in the header as it already does in the Rich
    pre-chat panel / plain-prompt status bar.
    """
    for status, meta in layout_module.STATE_META.items():
        layout.state.current_status = status
        fragments = layout._create_header().content.text()
        color = meta[2]
        styles = [s for s, _t in fragments if s.startswith(f"fg:{color}")]
        assert styles, f"status {status!r} should render in fg:{color}"


def test_owner_mode_badge_only_shown_when_owner_mode(layout):
    """The owner-mode badge appears in the header iff ``owner_mode`` is True."""
    layout.owner_mode = False
    fragments_off = layout._create_header().content.text()
    text_off = "".join(t for _s, t in fragments_off)
    assert "OWNER" not in text_off

    layout.owner_mode = True
    fragments_on = layout._create_header().content.text()
    text_on = "".join(t for _s, t in fragments_on)
    assert "OWNER" in text_on
    badge_styles = [s for s, t in fragments_on if "OWNER" in t]
    assert badge_styles and all(s.startswith(f"fg:{layout_module.AMBER}") for s in badge_styles)


def test_center_fragments_color_by_message_role(layout):
    """Scrollback lines are coloured per role, matching ``_ROLE_COLORS``."""
    layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content="hi there"))
    layout.state.messages.append(ChatMessage(role=ChatMessageRole.ASSISTANT, content="hello"))
    layout.update_from_state()

    fragments = layout._get_center_fragments()
    joined_styles = {style for style, _text in fragments}
    user_color = layout_module._ROLE_COLORS["user"]
    assistant_color = layout_module._ROLE_COLORS["assistant"]
    assert any(s.startswith(f"fg:{user_color}") for s in joined_styles)
    assert any(s.startswith(f"fg:{assistant_color}") for s in joined_styles)

    # Plain-text content is unaffected (same text, same wrapping) — only the
    # rendering path gained colour.
    plain = layout._get_center_content()
    colored_text = "".join(t for _s, t in fragments)
    assert plain == colored_text


def test_center_fragments_empty_history_shows_colored_welcome(layout):
    """No messages yet: the welcome line renders, now with a static accent colour."""
    fragments = layout._get_center_fragments()
    assert len(fragments) == 1
    style, text = fragments[0]
    assert "Добро пожаловать" in text
    assert style.startswith(f"fg:{layout_module.ACCENT}")


def test_app_style_recolors_menu_and_scrollbar_not_defaults():
    """The Application-wide Style overrides prompt_toolkit's grey scrollbar/menu.

    Regression guard: without an explicit ``style=`` the defaults are
    ``scrollbar.background: bg:#aaaaaa`` / ``scrollbar.button: bg:#444444``
    and ``menu: bg:#888888 #ffffff`` (prompt_toolkit's built-in
    ``default_ui_style()``) — flat mid-grey, unrelated to the brand.  This
    only asserts our override is present and on-brand; it does not touch
    ``PROMPT_TOOLKIT_NO_CPR`` or any other stabilization-sensitive setting.
    """
    attrs = layout_module._APP_STYLE.style_rules
    as_dict = dict(attrs)
    assert as_dict["menu"].strip() != "bg:#888888 #ffffff"
    assert layout_module.VIOLET.lower() in as_dict["scrollbar.button"].lower()
    assert layout_module.MUTED_GREY.lower() in as_dict["scrollbar.background"].lower()


def test_update_new_events_indicator_follow_mode(layout):
    """In auto-follow mode new events never accumulate a badge."""
    layout.state.events = [{"event": "test"}, {"event": "test2"}]
    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 0
    assert layout.last_event_count == 2


def test_update_new_events_indicator_frozen_mode(layout):
    """While the viewport is frozen, new events accumulate into the badge."""
    layout.auto_follow = False
    layout.state.events = [{"event": "test"}]
    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 1
    layout.state.events.append({"event": "test2"})
    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 2
    # No new events → badge unchanged
    layout._update_new_events_indicator()
    assert layout.new_events_indicator == 2


def test_update_new_messages_frozen_mode(layout):
    """Messages arriving while frozen accumulate into the badge."""
    layout.auto_follow = False
    layout.state.messages.append(ChatMessage(role=ChatMessageRole.INFO, content="one"))
    layout._update_new_messages()
    assert layout.new_message_count == 1
    layout.state.messages.append(ChatMessage(role=ChatMessageRole.INFO, content="two"))
    layout._update_new_messages()
    assert layout.new_message_count == 2


def test_header_badge_shows_accumulated_updates(layout):
    """Frozen viewport → header shows a ``↓N новых`` badge."""
    layout.auto_follow = False
    layout.new_message_count = 3
    assert "↓3 новых" in layout._header_extra()

    layout.auto_follow = True
    assert "↓" not in layout._header_extra()


def test_scroll_functions(layout):
    """Scroll up freezes the viewport; scrolling to the bottom resumes follow."""
    # Fill history so there is something to scroll (30 lines > 20 visible).
    for i in range(30):
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content=f"m{i}"))
    layout.update_from_state()

    # Bottom (auto-follow) shows the last 20 lines.
    assert layout.auto_follow is True
    assert layout.scroll_offset == 0
    bottom = layout._get_center_content()
    assert "m29" in bottom and "m0" not in bottom

    # One line up → manual mode, frozen at offset 1.
    layout.scroll_line_up()
    assert layout.auto_follow is False
    assert layout.scroll_offset == 1
    frozen = layout._get_center_content()
    assert "m28" in frozen and "m29" not in frozen

    # New content while frozen does not move the viewport.
    layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content="m30"))
    layout.update_from_state()
    assert layout.scroll_offset == 1
    assert "m30" not in layout._get_center_content()

    # Scrolling back down resumes auto-follow and clears the badge.
    layout.scroll_line_down()
    assert layout.auto_follow is True
    assert layout.scroll_offset == 0
    assert "m30" in layout._get_center_content()


def test_scroll_clamps_to_content(layout):
    """Scroll offset never exceeds the content bounds."""
    for i in range(25):
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content=f"m{i}"))
    layout.update_from_state()
    layout.scroll_to_top()
    assert layout.auto_follow is False
    assert layout.scroll_offset == layout.max_scroll
    assert layout.max_scroll == 25 - layout._center_height()

    # Repeated scroll up at the top does not overrun.
    layout.scroll_line_up()
    layout.scroll_line_up()
    assert layout.scroll_offset == layout.max_scroll


def test_scroll_to_bottom_resumes_follow(layout):
    """End/home semantics: scroll_to_bottom clears the badge and follows."""
    for i in range(30):
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content=f"m{i}"))
    layout.update_from_state()
    layout.scroll_line_up()
    layout.new_message_count = 4
    layout.scroll_to_bottom()
    assert layout.auto_follow is True
    assert layout.scroll_offset == 0
    assert layout.new_message_count == 0


def test_center_content_wraps_long_lines(layout):
    """Long lines wrap deterministically so scroll math matches rendering."""
    layout.state.messages.append(
        ChatMessage(role=ChatMessageRole.USER, content="x" * 200)
    )
    layout.update_from_state()
    lines = layout._get_center_content().splitlines()
    assert all(layout_module.cell_len(line) <= 77 for line in lines)
    # 200 cells / 77 per line → 3 wrapped lines; viewport shows all of them.
    assert len(lines) == 3
    assert layout.max_scroll == 0


def test_page_scroll(layout):
    """Page up/down scroll by a full viewport height."""
    for i in range(40):
        layout.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content=f"m{i}"))
    layout.update_from_state()
    layout.scroll_page_up()
    assert layout.auto_follow is False
    assert layout.scroll_offset == layout._center_height()
    layout.scroll_page_down()
    assert layout.auto_follow is True
    assert layout.scroll_offset == 0


def test_key_bindings_allow_scrolling_in_any_state(layout):
    """Test that key bindings allow scrolling in any state."""
    layout.state.current_status = "planning"
    assert hasattr(layout, 'scroll_line_up')
    assert hasattr(layout, 'scroll_line_down')
    assert hasattr(layout, 'scroll_page_up')
    assert hasattr(layout, 'scroll_page_down')
    assert hasattr(layout, 'scroll_to_top')
    assert hasattr(layout, 'scroll_to_bottom')


def test_enlarged_input_box_and_menu_float_position(layout):
    """Input box height is 3 lines and slash menu float is positioned right above status bar."""
    layout._create_layout()
    assert layout._input_height() == 3
    assert layout.input_window is not None
    assert layout.input_window.window.height == 3
    assert layout.menu_float is not None
    assert layout.menu_float.bottom == 1 + layout._input_height()


def test_slash_menu_keyboard_navigation_move(layout):
    """Slash menu navigation updates menu_index properly."""
    layout.catalog = (
        layout_module.SlashCommand(name="/clear", description="Clear screen"),
        layout_module.SlashCommand(name="/theme", description="Change theme"),
    )
    layout._current_buffer = lambda: _FakeBuf("/")
    layout._update_menu_visibility()
    assert layout.menu_visible is True
    assert layout.menu_index == 0

    layout._menu_move(1)
    assert layout.menu_index == 1

    layout._menu_move(-1)
    assert layout.menu_index == 0


if __name__ == "__main__":
    pytest.main([__file__])
