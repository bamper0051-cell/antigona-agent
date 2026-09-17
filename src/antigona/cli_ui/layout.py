"""Stable three-zone full-screen layout for the Antigona CLI.

Zones (top → bottom):
  header — fixed 2-line banner: face + identity + connection on line 1, status
           + active flow + new-events indicator on line 2.  Static, never animates.
  center — scrollable chat history; the only resizing zone.
  status — fixed 1-line pipeline bar (phase + last real events + live activities).
  input  — fixed 1-line TextArea.
  menu   — a Float overlay just above the status line; visible while the input
           buffer starts with ``/`` (compact command list + help card for the
           selected command).  Never pushes the input line or redraws history.

Rendering contract (Termux / SSH / narrow terminals / resize-safe):
  * prompt_toolkit owns the alternate screen and the cursor; Rich is never used
    while this layout runs (see ``LayoutRendererAdapter`` in
    ``layout_renderer.py``) — the historical source of flicker and CPR noise.
  * Content getters are PURE: they never mutate animation, scroll or indicator
    state, so a repaint triggered by a cursor flash, a keypress or a resize can
    never change the frame *by itself* (no hidden frame counter, no timer).
    They may still read already-changed input, e.g. the gaze glyph below
    reads the input buffer's current cursor column — that's a read of state
    that genuinely changed (the user moved the cursor), not a self-driven
    animation.  All *persisted* state mutation happens in the bounded refresh
    loop (``_refresh_loop``), which invalidates the application only when the
    render key actually changed — idle sessions repaint nothing and burn no CPU.
  * The face is a single fixed-width glyph per state (the multi-frame portrait
    was reverted before; frame-cycling inside a content getter made every
    repaint mutate the frame).  The only animation is the status-bar spinner,
    ticking at a bounded rate (4 Hz) while real work is in flight, and stopping
    the instant the work ends.
  * The face carries one *event-driven* modifier: a fixed-width gaze glyph
    (``_get_gaze_glyph``) that tracks the input cursor's column, so the eyes
    appear to follow where the owner is typing (left / center-down / right).
    It adds no timer and no new invalidation point — prompt_toolkit already
    invalidates the whole layout on every buffer text/cursor change
    (``BufferControl.get_invalidate_events`` wires ``on_text_changed`` and
    ``on_cursor_position_changed`` into ``Application.invalidate()``), so the
    header simply reads the already-current cursor position on a repaint that
    was going to happen anyway.

Manual scroll mode (spec: "Режим ручной прокрутки"):
  * Any scroll up freezes the viewport (``auto_follow`` → False) and keeps the
    position fixed while new messages/events arrive.
  * While frozen, the header shows a ``↓N новых`` indicator.
  * Scrolling down to the bottom (or End / /clear) resumes auto-follow and
    shows the accumulated updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable, Sequence
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from rich.cells import cell_len

from antigona.cli_ui import themes
from antigona.cli_ui.activity import get_tracker
from antigona.cli_ui.command_menu import build_command_help, merge_catalog
from antigona.cli_ui.commands import CommandKind, parse_command
from antigona.cli_ui.models import ChatUIState

# AURORA palette (single source of truth: antigona.cli_ui.panel).  Reused
# verbatim rather than re-invented so the prompt_toolkit full-screen layout
# and the Rich-based pre-chat banner/panel/status-bar read as one brand, not
# two different colour schemes stitched together.
from antigona.cli_ui.panel import (
    ACCENT,
    AMBER,
    BANNER_COLORS,
    BLUE,
    MAGENTA,
    MUTED_GREY,
    MUTED_LAVENDER,
    RED,
    STATE_META,
    VIOLET,
)
from antigona.cli_ui.portrait import (
    GAZE_TO_BIAS,
    GazeSmoother,
    PortraitEngine,
)
from antigona.cli_ui.portrait_engine import portrait_mode_for_status
from antigona.cli_ui.prompts import SlashCommand, read_prompt
from antigona.cli_ui.status_bar import build_pipeline_bar

logger = logging.getLogger(__name__)

#: Fixed width (in terminal cells) of the face glyph — the header must never
#: resize when the state changes (single-cell glyphs are padded to match).
_FACE_WIDTH_CELLS = 5

#: Cap for the in-memory Up/Down recall history (avoids unbounded growth).
_MAX_INPUT_HISTORY: int = 200

#: How long the gaze stays where it was when Enter was pressed, before the eyes
#: glide back to centre and the working animation is allowed to start.
_PORTRAIT_HOLD_SECONDS: float = 0.55

#: Input length (characters) above which the eyes take on the concentrated
#: glyphs.  Deliberately a *micro* reaction: two cells, nothing else.
_INPUT_LENGTH_FOCUS_THRESHOLD: int = 20

#: Per-second ramp of ``working_intensity`` (≈0.7 s to fully engage or relax).
_WORKING_INTENSITY_RATE: float = 1.4

#: One calm glyph per face state.  Deliberately single-frame: the reverted
#: multi-frame portrait cycled frames inside the header getter, which made
#: every repaint mutate state (render churn / CPR noise in Termux).
_FACE_GLYPHS: dict[str, str] = {
    "IDLE": "[● ●]",
    "TYPING": "[◍ ◍]",
    "PLANNING": "[◎ ◎]",
    "READING": "[◔ ◔]",
    "WRITING": "[✍ ✍]",
    "RUNNING_TOOL": "[⚙ ⚙]",
    "RUNNING_COMMAND": "[💻]",
    "TESTING": "[🧪]",
    "VERIFYING": "[🛡 🛡]",
    "WAITING_USER": "[🔐]",
    "SUCCESS": "[✿ ✿]",
    "WARNING": "[▲ ▲]",
    "ERROR": "[✖ ✖]",
}

#: Fixed width (in terminal cells) of the gaze indicator appended to the face
#: — like the face itself, every variant must occupy the same width so the
#: header never resizes when the gaze direction changes.
_GAZE_WIDTH_CELLS = 1

#: Eyes tracking the input cursor — a *modifier* of the face glyph, not a
#: replacement for the state machine above.  Three fixed-width variants,
#: bucketed by where the cursor sits in the typed input text: looking left,
#: looking down/center at the input line (also the default while the buffer
#: is empty), looking right.  Deliberately no fourth "typing" frame and no
#: timer: the bucket is recomputed from ``buffer.cursor_position`` inside the
#: header's own (already pure) content getter, which prompt_toolkit already
#: re-invokes on every keystroke — ``BufferControl.get_invalidate_events()``
#: wires ``buffer.on_text_changed``/``on_cursor_position_changed`` straight
#: into ``Application.invalidate()``. No new tick, no new redraw loop.
_GAZE_LEFT = "◄"
_GAZE_CENTER = "▼"
_GAZE_RIGHT = "►"

#: Panel status → face state (unchanged from the original state machine).
_STATUS_TO_FACE: dict[str, str] = {
    "idle": "IDLE",
    "sending": "TYPING",
    "planning": "PLANNING",
    "tool_executing": "RUNNING_TOOL",
    "observing": "READING",
    "verifying": "VERIFYING",
    "waiting_approval": "WAITING_USER",
    "done": "SUCCESS",
    "failed": "ERROR",
    "cancelled": "ERROR",
    "reconnecting": "RUNNING_COMMAND",
    "disconnected": "ERROR",
    "timeout": "WARNING",
    "error": "ERROR",
}

#: Statuses that must never drive the spinner (calm, static display).
_CALM_STATUSES: frozenset[str] = frozenset(
    {"idle", "done", "failed", "cancelled", "disconnected"}
)

# ── Colour (static, no new timer — see module docstring / stabilization task) ─
#
# The whole point of this section: every colour used below is either the
# brand violet or one of the AURORA accents already shipping in
# ``antigona.cli_ui.panel``/``status_bar`` (imported above), never a new hex
# value invented just for this file.  That keeps the full-screen layout,
# the Rich pre-chat banner/panel and the plain-prompt status bar looking
# like one interface instead of three palettes bolted together.
#
# All of it is applied as ``fg:#RRGGBB`` (optionally ``bold``) fragments on
# top of each Window's own base ``style=``; prompt_toolkit composes
# ``parent_style + window.style + fragment.style`` per cell (verified via
# ``Window._write_to_screen_at_index`` /  ``_apply_style``), so a bare ``fg:``
# fragment safely overrides only the foreground and never erases a Window's
# background.  Nothing here is animated: every value below is computed once
# per (already pure) content-getter call from state that already changed —
# no new tick, no new invalidation source (see ``test_gaze_no_new_animation_
# timer_introduced`` in ``tests/unit/test_cli_ui_layout/test_layout.py``,
# extended by ``test_no_new_animation_timer_for_color`` to cover this block).

#: Status → accent colour, reusing the exact colour slot ``panel.STATE_META``
#: already assigns per status (so the header's status word and the Rich
#: panel's status dot are always the same colour for the same status).
_STATUS_COLOR: dict[str, str] = {status: meta[2] for status, meta in STATE_META.items()}

#: Connection indicator → accent colour (mirrors the emoji semantics already
#: used in ``_get_connection_text``: green/amber/red/grey).
_CONNECTION_COLOR: dict[str, str] = {
    "connected": ACCENT,
    "reconnecting": AMBER,
    "disconnected": RED,
}

#: Message role → accent colour in the scrollback history.  Human input and
#: Antigona's own replies get the two most distinct hues (steel blue vs. the
#: brand violet); everything else is a supporting/meta tone.
_ROLE_COLORS: dict[str, str] = {
    "user": BLUE,
    "assistant": VIOLET,
    "system": MUTED_LAVENDER,
    "tool": ACCENT,
    "error": RED,
    "warning": AMBER,
    "info": MAGENTA,
}

#: Dark-plum backdrop for the ``/`` menu overlay and a violet-tinted
#: scrollbar — replaces prompt_toolkit's built-in mid-grey defaults
#: (``bg:#888888`` menu, ``#aaaaaa``/``#444444`` scrollbar) with the same
#: AURORA family used everywhere else.  Explicit fg/bg pairs (not just fg)
#: here on purpose: both are small fixed-size decorative surfaces meant to
#: look the same regardless of the host terminal's own background.
_APP_STYLE = Style.from_dict(
    {
        "menu": "bg:#241B36 fg:#F5F3FF",
        "scrollbar.background": f"bg:{MUTED_GREY}",
        "scrollbar.button": f"bg:{VIOLET}",
    }
)

#: The header Window's own background (also the ``Window(style=...)`` value
#: in ``_create_header``) — pulled out as a constant so the wordmark-gradient
#: contrast fix below (``_wordmark_color``) can compare against it by name
#: instead of a second hard-coded literal drifting out of sync.
_HEADER_BG = VIOLET


def _wordmark_color(color: str, header_bg: str | None = None) -> str:
    """Gradient stop for the header wordmark, guarding against zero contrast.

    ``panel.BANNER_COLORS`` includes VIOLET as one of its 8 stops (the
    letter "I"), which is fine against the Rich banner's plain background —
    but the header here fills its own background with that same VIOLET, so
    used unmodified that one letter would be invisible.  Falls back to the
    header's own foreground white in that one case; every other stop passes
    through untouched.
    """
    return "#ffffff" if color.upper() == (header_bg or _HEADER_BG).upper() else color


#: Maximum command rows in the ``/`` menu (help card is separate).
_MAX_MENU_ITEMS = 8

#: Refresh cadence: bounded, Termux-friendly.  0.25s while work/animations run
#: (4 Hz spinner), 0.5s otherwise.  Idle never repaints (render-key dedup).
_ACTIVE_REFRESH = 0.25
_CALM_REFRESH = 0.5
_SPINNER_TICK_HZ = 4


def _terminal_size() -> tuple[int, int]:
    """Return (columns, rows) of the terminal, with sane fallbacks."""
    size = shutil.get_terminal_size((80, 24))
    return size.columns, size.lines


def _wrap_text(text: str, width: int) -> list[str]:
    """Wrap a string to *width* terminal cells (deterministic char-level wrap).

    Used both by the scroll math and the rendered content, so the viewport
    slice always matches what the window paints (the window's own wrap is a
    no-op for lines that already fit).
    """
    if width <= 0:
        return [text]
    lines: list[str] = []
    current = ""
    current_w = 0
    for char in text:
        w = cell_len(char)
        if current_w + w > width:
            lines.append(current)
            current = char if w <= width else ""
            current_w = w if w <= width else 0
            continue
        current += char
        current_w += w
    if current or not lines:
        lines.append(current)
    return lines


def _pad_cells(text: str, width: int) -> str:
    """Pad text to exactly *width* terminal cells (right side)."""
    gap = max(0, width - cell_len(text))
    return text + " " * gap


class PickerKeyBridge:
    """Route approval-picker keys through the layout's key bindings.

    The approval picker historically read keys via a nested PromptSession,
    which fights the full-screen Application (alternate screen, CPR probes,
    duplicated input).  When the layout runs, the picker instead awaits keys
    pushed here by the layout's key bindings — one terminal, one input owner.
    """

    def __init__(self) -> None:
        self.active = False
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def next_key(self) -> str:
        """Wait for the next key pushed by the layout."""
        return await self._queue.get()

    def push(self, key: str) -> None:
        """Push a key name (``up``/``down``/``enter``/``y``/…) for the picker."""
        self._queue.put_nowait(key)

    def __enter__(self) -> PickerKeyBridge:
        self.active = True
        return self

    def __exit__(self, *exc: Any) -> None:
        self.active = False


class AntigonaLayout:
    """Three-zone full-screen layout: fixed header, scrollable center, fixed input.

    The single UI for ``antigona chat``: every presentation call from the
    ChatController is routed here (``LayoutRendererAdapter``); this class owns
    the alternate screen, the cursor, the scroll state and the ``/`` menu.
    """

    def __init__(
        self,
        state: ChatUIState,
        renderer: Any,
        on_input: Callable[[str], Any],
        on_key: Callable[[str], Any] | None = None,
        catalog: Sequence[SlashCommand] | None = None,
        key_bridge: PickerKeyBridge | None = None,
    ) -> None:
        self.state = state
        self.renderer = renderer
        self.on_input = on_input
        self.on_key = on_key
        self.key_bridge = key_bridge
        self.catalog: tuple[SlashCommand, ...] = tuple(catalog) if catalog else merge_catalog(None)

        # Layout components (created lazily in _create_layout).
        self.header_window: Window | None = None
        self.portrait_window: Window | None = None
        self.center_window: Window | None = None
        self.status_window: Window | None = None
        self.input_window: TextArea | None = None
        self.menu_float: Float | None = None
        self.container: FloatContainer | None = None
        self.application: Application[Any] | None = None

        # ── Scroll state ─────────────────────────────────────────────────────
        #: Lines scrolled up from the bottom (0 = bottom, auto-follow).
        self.scroll_offset = 0
        #: False while the user is reading older history (viewport frozen).
        self.auto_follow = True
        #: Maximum scrollable offset (content lines − viewport height).
        self.max_scroll = 0
        #: Messages/events accumulated while frozen (the ``↓N новых`` badge).
        self.new_message_count = 0
        self.new_events_indicator = 0
        self._last_message_count = 0
        self.last_event_count = 0

        # ── Face state + living portrait (presentation-only) ───────────────
        self.current_face_state = "IDLE"
        self.portrait_engine = PortraitEngine()
        self._portrait_phase = 0

        # ── Gaze hysteresis (prevents eye jitter at cursor bucket boundaries) ─
        #: Last committed gaze direction (seven-way bucket string).
        self._portrait_gaze_committed: str = "center"
        #: Cursor fraction at last committed gaze change.
        self._portrait_gaze_last_fraction: float = 0.5
        #: Minimum fraction movement required to switch to a new gaze bucket.
        _GAZE_DEAD_ZONE: float = 0.08
        self._portrait_gaze_dead_zone = _GAZE_DEAD_ZONE
        #: Eases the *rendered* bucket towards the committed one (~150 ms).
        #: Hysteresis stays upstream (it decides the target); the smoother only
        #: decides how fast the eyes travel there.
        self.gaze_smoother = GazeSmoother()
        #: Monotonic timestamp of the last smoother/choreography advance.
        self._portrait_last_tick: float = time.monotonic()

        # ── Enter choreography (hold → return to centre → working) ───────────
        #: Active choreography phase: "hold" | "return_center" | "working" | None.
        self.portrait_current_phase: str | None = None
        #: Bumped by every submit/state change so a stale phase can never win.
        self.portrait_choreo_generation: int = 0
        #: Gaze bucket at the instant Enter was pressed (drives the working FX).
        self.last_gaze_before_submit: str = "center"
        #: Monotonic deadline of the "hold" phase.
        self._portrait_hold_until: float = 0.0
        #: Working FX ramp, 0 (calm) … 1 (fully engaged).
        self.working_intensity: float = 0.0

        # ── Slash menu state ─────────────────────────────────────────────────
        self.menu_visible = False
        self.menu_index = 0

        # ── In-memory input history (Up/Down recall) ────────────────────────
        #: Submitted lines (newest last), recalled by the arrow keys.
        self._input_history: list[str] = []
        #: Position in recall history; None = at the live working line.
        self._history_pos: int | None = None
        #: Unsent text preserved while walking back through history.
        self._working_line: str = ""

        # ── Refresh loop ─────────────────────────────────────────────────────
        self._last_render_key: tuple[Any, ...] | None = None
        self._refresh_task: asyncio.Task[None] | None = None

        # Owner Mode state
        self.owner_mode = False
        self.pin_attempts = 0
        self.max_pin_attempts = 3

        # Key bindings
        self.kb = KeyBindings()
        self._setup_key_bindings()

        # ── Active colour theme (terminal "skin", /theme) ───────────────────
        self.theme = themes.get_active()
        self.app_style = themes.build_style(self.theme)

    # ── Key bindings ─────────────────────────────────────────────────────────

    def _setup_key_bindings(self) -> None:
        """Bind scrolling, the ``/`` menu and the approval-picker bridge.

        Precedence (prompt_toolkit takes the first matching binding):
        1. picker bridge (approval decision keys),
        2. slash menu (only while ``menu_visible``),
        3. scrolling / navigation (always available).
        """
        picker_filter = Condition(lambda: self.key_bridge is not None and self.key_bridge.active)
        menu_filter = Condition(lambda: self._is_menu_active())
        scrolling_filter = Condition(
            lambda: not self._is_menu_active() and (self.key_bridge is None or not self.key_bridge.active)
        )

        # 1. Approval picker keys — routed to the bridge, never to the UI.
        for _key in ("up", "down", "left", "right", "enter", "escape", "y", "Y", "n", "N", "d", "D"):

            def _push_picker_key(event: Any, _key: str = _key) -> None:
                if self.key_bridge is not None:
                    self.key_bridge.push(_key)

            self.kb.add(_key, filter=picker_filter)(_push_picker_key)

        # 2. Slash menu navigation / activation (only while visible).
        @self.kb.add("up", filter=menu_filter)
        @self.kb.add("s-tab", filter=menu_filter)
        def _menu_up(event: Any) -> None:
            self._menu_move(-1)

        @self.kb.add("down", filter=menu_filter)
        def _menu_down(event: Any) -> None:
            self._menu_move(1)

        @self.kb.add("tab", filter=menu_filter)
        def _menu_tab(event: Any) -> None:
            self._menu_complete(event)

        @self.kb.add("escape", filter=menu_filter)
        def _menu_escape(event: Any) -> None:
            self.menu_visible = False
            self.request_repaint()

        @self.kb.add("enter", filter=menu_filter)
        def _menu_enter(event: Any) -> None:
            self._menu_activate(event)

        # 3. Scrolling — only when picker and menu are closed.
        @self.kb.add("pageup", filter=scrolling_filter)
        def _(event: Any) -> None:
            self.scroll_page_up()

        @self.kb.add("pagedown", filter=scrolling_filter)
        def _(event: Any) -> None:
            self.scroll_page_down()

        @self.kb.add("up", filter=scrolling_filter)
        def _up_recall(event: Any) -> None:
            # Стрелка вверх: подставить предыдущее отправленное сообщение/команду.
            self._history_previous(event)

        @self.kb.add("down", filter=scrolling_filter)
        def _down_recall(event: Any) -> None:
            # Стрелка вниз: движение вперёд по истории (к текущей строке).
            self._history_next(event)

        @self.kb.add("c-up", filter=scrolling_filter)
        @self.kb.add("escape", "up", filter=scrolling_filter)
        def _scroll_up(event: Any) -> None:
            # Ctrl+Up / Esc+Up: построчный скролл истории чата.
            self.scroll_line_up()

        @self.kb.add("c-down", filter=scrolling_filter)
        @self.kb.add("escape", "down", filter=scrolling_filter)
        def _scroll_down(event: Any) -> None:
            # Ctrl+Down / Esc+Down: построчный скролл вниз.
            self.scroll_line_down()

        @self.kb.add(Keys.ScrollUp, filter=scrolling_filter)
        def _(event: Any) -> None:
            self.scroll_line_up()

        @self.kb.add(Keys.ScrollDown, filter=scrolling_filter)
        def _(event: Any) -> None:
            self.scroll_line_down()

        @self.kb.add("home", filter=scrolling_filter)
        def _(event: Any) -> None:
            self.scroll_to_top()

        @self.kb.add("end", filter=scrolling_filter)
        def _(event: Any) -> None:
            self.scroll_to_bottom()

        @self.kb.add("escape", filter=scrolling_filter)
        def _(event: Any) -> None:
            # No menu open: escape is inert (never clears typed input).
            pass

        # 4. Ctrl+C exits the application. prompt_toolkit's full-screen
        # Application does not bind this by default, so without an explicit
        # handler Ctrl+C is silently swallowed (reported bug: "Ctrl+c не
        # закрывает cli").
        @self.kb.add("c-c")
        def _(event: Any) -> None:
            event.app.exit()

    # ── Zone builders ────────────────────────────────────────────────────────

    def _create_header(self) -> Window:
        """Fixed 3-line mini-monitoring dashboard banner:
        Line 1: Logo + Gateway URL + Connection Status + Owner/User Badge + CPU Telemetry
        Line 2: Primary Model + Verifier Status + Session ID + Active Flows + Pending Approvals + Event Counter
        Line 3: Face Glyph + Gaze + Current Status + Extra Updates Indicator
        """
        def get_header_content() -> StyleAndTextTuples:
            face = self._get_antigona_face()
            gaze = self._get_gaze_glyph()
            conn = self._get_connection_text()
            conn_color = themes.connection_color(self.theme, self.state.connection)
            status = self._get_status_text()
            status_color = themes.status_color(self.theme, self.state.current_status)
            extra = self._header_extra()
            extra_color = self._header_extra_color()

            # Live telemetry details
            model_name = os.environ.get("ANTIGONA_MODEL_PRIMARY", "deepseek-chat").split("/")[-1]
            sess_id = self.state.session_id or "cli-session"
            flows_count = len(self.state.active_flows)
            approvals_count = len(self.state.pending_approvals)
            events_count = len(self.state.events)
            gw_url = self.state.gateway_url or os.environ.get("ANTIGONA_GATEWAY_URL", "127.0.0.1:8090")
            gw_short = gw_url.replace("http://", "").replace("https://", "")

            # Line 1: Logo + Gateway + Mode + System CPU
            fragments: StyleAndTextTuples = [("", "⚡ ")]
            fragments.extend(
                (f"fg:{_wordmark_color(color, self.theme.header_bg)}", letter) for letter, color in BANNER_COLORS
            )
            fragments.append(("", "  "))
            fragments.append((f"fg:{conn_color}", f"🌐 GW: {gw_short} ({conn})"))
            if self.owner_mode:
                fragments.append(("", "  "))
                fragments.append((f"fg:{AMBER} bold", "👑 OWNER ELEVATED"))
            else:
                fragments.append(("", "  "))
                fragments.append((f"fg:{MUTED_LAVENDER}", "👤 USER MODE"))
            
            try:
                load_val = os.getloadavg()[0]
                sys_badge = f"  💻 CPU: {load_val:.2f}"
            except Exception:
                sys_badge = "  💻 SYS: OK"
            fragments.append((f"fg:{ACCENT}", sys_badge))

            fragments.append(("", "\n"))

            # Line 2: Mini Monitoring Panel (Model, Verifier, Session, Flows, Approvals, Events)
            fragments.append((f"fg:{MAGENTA} bold", f"🧠 MODEL: {model_name}"))
            fragments.append(("", " │ "))
            fragments.append((f"fg:{ACCENT}", "🛡️ VERIFIER: ACTIVE"))
            fragments.append(("", " │ "))
            fragments.append((f"fg:{MUTED_LAVENDER}", f"💬 SESS: {sess_id}"))
            fragments.append(("", " │ "))
            fragments.append((f"fg:{AMBER if flows_count > 0 else MUTED_LAVENDER}", f"🔄 FLOWS: {flows_count}"))
            fragments.append(("", " │ "))
            fragments.append((f"fg:{RED if approvals_count > 0 else MUTED_LAVENDER}", f"🔐 APPR: {approvals_count}"))
            fragments.append(("", " │ "))
            fragments.append((f"fg:{VIOLET}", f"📊 EV: {events_count}"))

            fragments.append(("", "\n"))

            # Line 3: Face Glyph + Gaze + Current Status
            fragments.append(("", f"{face}{gaze} "))
            fragments.append((f"fg:{status_color} bold", status))
            if extra:
                fragments.append((f"fg:{extra_color}", f" {extra}"))
            return fragments

        return Window(
            FormattedTextControl(get_header_content),
            height=3,
            style=f"bg:{self.theme.header_bg} fg:#ffffff",
            dont_extend_height=True,
        )

    def _header_extra_color(self) -> str:
        """Accent for ``_header_extra()``: amber while updates are pending."""
        pending = self.new_message_count + self.new_events_indicator
        if pending > 0 and not self.auto_follow:
            return self.theme.amber
        return self.theme.muted_lavender

    def _portrait_profile(self) -> Any | None:
        """Responsive portrait profile for the current terminal geometry."""
        cols, rows = _terminal_size()
        return self.portrait_engine.profile_for_terminal(cols, rows)

    def _portrait_height(self) -> int:
        """Rows reserved for the portrait and status banner box; zero on tiny terminals."""
        profile = self._portrait_profile()
        if profile is None:
            return 0
        return int(profile.rows) + 4 + (1 if self._portrait_debug_enabled() else 0)

    @staticmethod
    def _portrait_debug_enabled() -> bool:
        """True when ``ANTIGONA_PORTRAIT_DEBUG`` asks for the diagnostics line."""
        return os.getenv("ANTIGONA_PORTRAIT_DEBUG", "0").strip().lower() in {
            "1",
            "on",
            "true",
            "yes",
        }

    def _input_length(self) -> int:
        """Length of the text currently sitting in the input buffer."""
        buf = self._current_buffer()
        return len(buf.text) if buf is not None and buf.text else 0

    def _portrait_mode(self) -> str:
        """Winning portrait mode: working > monitoring > typing > idle."""
        return portrait_mode_for_status(
            self.state.current_status,
            typing=self._input_length() > 0,
        )

    def _portrait_gaze_locked(self) -> bool:
        """True while the gaze belongs to the choreography, not to the cursor."""
        if self.portrait_current_phase in {"hold", "return_center", "working"}:
            return True
        return self._portrait_mode() in {"working", "monitoring"}

    def _portrait_gaze(self) -> str:
        """Seven-way gaze with hysteresis to prevent eye jitter at bucket edges.

        The raw cursor ratio is computed from the real prompt_toolkit buffer and
        then compared against the *last committed* fraction.  The gaze bucket
        changes only when the cursor has moved more than ``_GAZE_DEAD_ZONE``
        away from the last commit point, eliminating the left/center/left/center
        flicker that occurs when the cursor sits near a bucket boundary.

        The committed bucket is the *target*: it is handed to
        ``gaze_smoother``, which eases the rendered bucket towards it over
        ~150 ms (see ``_portrait_gaze_rendered``).  While the Enter
        choreography or real agent work owns the eyes, the cursor is ignored.
        """
        if self.portrait_current_phase == "hold":
            return self.last_gaze_before_submit
        if self._portrait_gaze_locked():
            self.gaze_smoother.force_gaze_target("center")
            return "center"

        buf = self._current_buffer()
        text = buf.text if buf is not None else ""
        if not text:
            self._portrait_gaze_committed = "center"
            self._portrait_gaze_last_fraction = 0.5
            self.gaze_smoother.set_gaze_target("center")
            return "center"
        position = int(getattr(buf, "cursor_position", 0)) if buf is not None else 0
        fraction = max(0.0, min(1.0, position / max(1, len(text))))
        delta = abs(fraction - self._portrait_gaze_last_fraction)
        new_gaze = self.portrait_engine.gaze_from_cursor(position, len(text))
        # Commit the new gaze only if we moved past the dead zone OR if the
        # candidate gaze is the same as the last committed one (hysteresis only
        # blocks *changes*, not confirmations of the current direction).
        if new_gaze == self._portrait_gaze_committed or delta >= self._portrait_gaze_dead_zone:
            self._portrait_gaze_committed = new_gaze
            self._portrait_gaze_last_fraction = fraction
        self.gaze_smoother.set_gaze_target(self._portrait_gaze_committed)
        return self._portrait_gaze_committed

    def _portrait_gaze_rendered(self) -> str:
        """The eased bucket actually drawn this frame (never a raw jump)."""
        target = self._portrait_gaze()
        if self.portrait_current_phase == "hold":
            # Frozen mid-transition: draw wherever the eyes currently are.
            return self.gaze_smoother.current_gaze_bucket()
        if self.gaze_smoother.at_target():
            return target
        return self.gaze_smoother.current_gaze_bucket()

    def _portrait_mood_pulse(self) -> str | None:
        """Short decorative work micro-expression, never a backend state claim."""
        forced = os.getenv("ANTIGONA_PORTRAIT_MOOD", "auto").strip().lower()
        if forced in {"laugh", "cry"}:
            return forced
        if forced in {"focus", "off", "none"}:
            return None
        if self.state.current_status not in {"running", "tool_executing", "observing"}:
            return None
        # One brief pulse in an 8-second cycle at 4 Hz. Stable choice per flow.
        if self._portrait_phase % 32 not in {16, 17}:
            return None
        token = self.state.active_flow_id or self.state.session_id or "antigona"
        checksum = sum(ord(ch) for ch in str(token))
        return "laugh" if checksum % 2 == 0 else "cry"

    def _portrait_expression(self) -> str:
        return self.portrait_engine.expression_for_status(
            self.state.current_status,
            mood_pulse=self._portrait_mood_pulse(),
        )

    def _portrait_input_focus(self) -> bool:
        """Micro-reaction: a long unsent line makes the eyes look concentrated.

        Only while the operator is actually typing — once real work starts, the
        agent status owns the face and this modifier is dropped.
        """
        if self._portrait_mode() != "typing":
            return False
        return self._input_length() >= _INPUT_LENGTH_FOCUS_THRESHOLD

    def _portrait_glitch_bias(self) -> str:
        """Which half of the face the working FX leans on.

        Antigona keeps working on the side she was last reading: the gaze at the
        moment Enter was pressed decides the bias, and it only applies while the
        working FX is actually ramped up.
        """
        if self.working_intensity <= 0.0:
            return "balanced"
        return GAZE_TO_BIAS.get(self.last_gaze_before_submit, "balanced")

    def begin_submit_choreography(self) -> int:
        """Start hold → return-to-centre → working after an input submit.

        Returns the new generation token.  Every call invalidates the previous
        chain, so a second Enter (or a state change) can never be overtaken by a
        stale phase.  No task and no sleep is created: the phases are advanced
        by the one bounded refresh loop that already runs.
        """
        self.portrait_choreo_generation += 1
        # A new generation starts clean: never carry a previous (or still
        # decaying) working ramp into hold/return_center, or the asymmetric FX
        # would fire before the eyes reach centre.
        self.working_intensity = 0.0
        self.last_gaze_before_submit = self._portrait_gaze_committed
        self.portrait_current_phase = "hold"
        self._portrait_hold_until = time.monotonic() + _PORTRAIT_HOLD_SECONDS
        # Freeze the eyes exactly where they were when Enter was pressed.
        self.gaze_smoother.locked = False
        self.gaze_smoother.force_gaze_target(self.last_gaze_before_submit)
        self.gaze_smoother.snap()
        self.gaze_smoother.locked = True
        return self.portrait_choreo_generation

    def cancel_submit_choreography(self) -> None:
        """Drop any running choreography and release the eyes back to the cursor."""
        self.portrait_choreo_generation += 1
        self.portrait_current_phase = None
        # Drop the working FX with the chain — no residual glitch bias.
        self.working_intensity = 0.0
        self.gaze_smoother.locked = False

    def _advance_submit_choreography(self, now: float, dt: float) -> None:
        """Move the Enter choreography one tick forward (called by the loop)."""
        phase = self.portrait_current_phase
        if phase is None:
            self.working_intensity = max(
                0.0, self.working_intensity - _WORKING_INTENSITY_RATE * dt
            )
            return

        if phase == "hold":
            if now >= self._portrait_hold_until:
                self.portrait_current_phase = "return_center"
                self.gaze_smoother.force_gaze_target("center")
            return

        if phase == "return_center":
            # WORKING starts only once the eyes have actually arrived.
            if self.gaze_smoother.at_target():
                self.portrait_current_phase = "working"
                # Seed the ramp so even an instantly-answered submit shows one
                # visible working beat instead of a silent phase flip.
                self.working_intensity = max(self.working_intensity, 0.25)
            return

        # phase == "working"
        if self._portrait_mode() in {"working", "monitoring"}:
            self.working_intensity = min(
                1.0, self.working_intensity + _WORKING_INTENSITY_RATE * dt
            )
            return
        # Real work finished (or never started): relax and hand the eyes back.
        self.working_intensity = max(
            0.0, self.working_intensity - _WORKING_INTENSITY_RATE * dt
        )
        if self.working_intensity <= 0.0:
            self.cancel_submit_choreography()

    def portrait_debug_state(self) -> dict[str, Any]:
        """Public debug snapshot (also rendered by ``ANTIGONA_PORTRAIT_DEBUG``)."""
        return {
            "current_state": self._portrait_mode(),
            "current_phase": self.portrait_current_phase,
            "current_gaze": self._portrait_gaze_rendered(),
            "target_gaze": self._portrait_gaze_committed,
            "last_gaze_before_submit": self.last_gaze_before_submit,
            "input_length": self._input_length(),
            "working_intensity": round(self.working_intensity, 2),
            "generation": self.portrait_choreo_generation,
        }

    def _get_portrait_fragments(self) -> StyleAndTextTuples:
        """Render the MASTER portrait into the real full-screen CLI.

        Pure read: all animation state is advanced only by ``_refresh_loop``.
        Cursor changes are already prompt_toolkit invalidation events, so gaze
        tracking adds no timer and never competes with the input widget.
        """
        profile = self._portrait_profile()
        if profile is None:
            return [("", "")]

        engine = os.getenv("ANTIGONA_PORTRAIT_ENGINE", "hybrid").strip().lower()
        if engine not in {"hybrid", "v1", "v2"}:
            engine = "hybrid"
        enabled = os.getenv("ANTIGONA_PORTRAIT_GLITCH", "1").strip().lower()
        glitch = enabled not in {"0", "off", "false", "no"}
        reduced_motion_env = os.getenv("ANTIGONA_REDUCED_MOTION", "0").strip().lower()
        reduced_motion = reduced_motion_env in {"1", "on", "true", "yes"}

        lines = self.portrait_engine.render(
            profile.name,
            gaze=self._portrait_gaze_rendered(),
            expression=self._portrait_expression(),
            phase=self._portrait_phase,
            engine=engine,  # type: ignore[arg-type]
            glitch=glitch,
            reduced_motion=reduced_motion,
            bias=self._portrait_glitch_bias(),
            input_focus=self._portrait_input_focus(),
            working_intensity=self.working_intensity,
        )
        cols, _rows = _terminal_size()
        left = max(0, (cols - profile.cols) // 2)
        indent = " " * left

        # Holographic Vertical Cyber-Scanline (Replaces rainbow shimmer wave).
        # Idle: Clean unified platinum silver with subtle breathing pulse.
        # Active: Sleek laser scanline beam sweeping top-to-bottom across the face.
        _COLOR_IDLE_BASE = "#E8E4DC"      # Platinum silver
        _COLOR_IDLE_BREATHE = "#C0B8D0"   # Soft lavender silver (breathe pulse)
        _COLOR_ACTIVE_BASE = "#A855F7"    # Brand Violet
        _COLOR_SCAN_BEAM = "#FFFFFF"      # Bright scanline beam
        _COLOR_SCAN_GLOW = "#38BDF8"      # Cyan halo around scanline

        is_active = self.state.current_status not in {"idle", "done"}

        fragments: StyleAndTextTuples = []
        if not is_active:
            # Idle state: Subtle unified breathing pulse (no line-by-line rainbow shift)
            breathe_phase = (self._portrait_phase // 4) % 2
            base_color = _COLOR_IDLE_BREATHE if breathe_phase == 1 else _COLOR_IDLE_BASE
            for line in lines:
                fragments.append((f"fg:{base_color}", f"{indent}{line}\n"))
        else:
            # Active state: Holographic vertical scanline sweep (moving laser beam)
            total_rows = len(lines)
            scan_row = (self._portrait_phase % total_rows) if total_rows > 0 else 0
            for line_idx, line in enumerate(lines):
                if line_idx == scan_row:
                    line_style = f"fg:{_COLOR_SCAN_BEAM} bold"
                elif abs(line_idx - scan_row) == 1:
                    line_style = f"fg:{_COLOR_SCAN_GLOW}"
                else:
                    line_style = f"fg:{_COLOR_ACTIVE_BASE}"
                fragments.append((line_style, f"{indent}{line}\n"))

        # Cybernetic Status Banner Box directly under Antigone's portrait
        banner_title = "❖ ANTIGONA AI OPERATING SYSTEM ❖  SOUL: GUARDIAN OF REASON"
        status_upper = self.state.current_status.upper()
        prof_upper = profile.name.upper()
        banner_sub = f"STATUS: {status_upper}  │  GATEWAY REALTIME ADAPTER  │  PORTRAIT: {prof_upper}"

        box_w = min(max(cols - 4, 10), max(profile.cols, len(banner_title) + 6))
        top_border = "╭" + "─" * (box_w - 2) + "╮"
        mid_1 = "│ " + banner_title.center(box_w - 4) + " │"
        mid_2 = "│ " + banner_sub.center(box_w - 4) + " │"
        bot_border = "╰" + "─" * (box_w - 2) + "╯"

        b_indent = " " * max(0, (cols - box_w) // 2)
        fragments.append((f"fg:{VIOLET}", f"{b_indent}{top_border}\n"))
        fragments.append(("fg:#FFFFFF bold", f"{b_indent}{mid_1}\n"))
        fragments.append((f"fg:{ACCENT}", f"{b_indent}{mid_2}\n"))
        fragments.append((f"fg:{VIOLET}", f"{b_indent}{bot_border}\n"))

        if self._portrait_debug_enabled():
            debug = self.portrait_debug_state()
            debug_line = "  ".join(f"{key}={value}" for key, value in debug.items())
            fragments.append((f"fg:{MUTED_GREY}", f"{b_indent}{debug_line}\n"))
        return fragments

    def _portrait_eye_color(self) -> str:
        """Slow pulsing eye accent for THINKING / WORKING states.

        Returns a hex colour for the eye glyph overlay.  During active work,
        the colour breathes through a dim-cyan → cyan → almost-white cycle
        approximately every 1.5 seconds.  No new timer is used — the phase is
        already advanced by the bounded refresh loop at 4 Hz.

        Returns an empty string when no eye-specific colour should be applied
        (the portrait will then use the single base portrait colour).
        """
        status = self.state.current_status
        if status in {"sending", "planning"}:
            # THINKING: slow dim-cyan → cyan pulse (period ≈ 1.5 s at 4 Hz = 6 ticks)
            phase = self._portrait_phase % 6
            _thinking_palette = (
                "#4A7FA5",  # dim cyan-blue
                "#5B9CBD",
                "#72B9D4",
                "#89CFEA",  # near-white cyan peak
                "#72B9D4",
                "#5B9CBD",
            )
            return _thinking_palette[phase]
        if status in {"running", "tool_executing", "observing"}:
            # WORKING: slightly warmer cyan → violet pulse
            phase = self._portrait_phase % 8
            _working_palette = (
                "#56A3C7",  # cyan
                "#7BBBD9",
                "#A0D0E6",  # near-white
                "#B8A0D9",  # start violet shift
                "#9080C8",  # violet
                "#7866B4",
                "#6E5AAA",
                "#5B4A9A",  # deep violet, back toward cyan next tick
            )
            return _working_palette[phase]
        return ""

    def _create_portrait(self) -> Window:
        """Responsive living portrait between the 2-line header and history."""
        return Window(
            FormattedTextControl(self._get_portrait_fragments),
            height=self._portrait_height,
            wrap_lines=False,
            dont_extend_height=True,
        )

    def _get_antigona_face(self) -> str:
        """Return the fixed-width portrait glyph for the current state."""
        face = _STATUS_TO_FACE.get(self.state.current_status, "IDLE")
        return _pad_cells(_FACE_GLYPHS.get(face, "🌿"), _FACE_WIDTH_CELLS)

    def _get_gaze_glyph(self) -> str:
        """Return the fixed-width gaze glyph for the current input cursor.

        Pure read of the focused input buffer's ``text``/``cursor_position``
        (the same ``_current_buffer()`` accessor the slash menu already uses)
        — nothing here is mutated or scheduled.  The header is already
        repainted on every keystroke because prompt_toolkit wires the
        buffer's own change events into ``Application.invalidate()``, so this
        only changes what that existing repaint draws; it adds no new timer
        and no new invalidation point.
        """
        buf = self._current_buffer()
        text = buf.text if buf is not None else ""
        if not text:
            glyph = _GAZE_CENTER
        else:
            position = getattr(buf, "cursor_position", 0)
            fraction = position / max(1, len(text))
            if fraction < 0.33:
                glyph = _GAZE_LEFT
            elif fraction > 0.66:
                glyph = _GAZE_RIGHT
            else:
                glyph = _GAZE_CENTER
        return _pad_cells(glyph, _GAZE_WIDTH_CELLS)

    def _get_status_text(self) -> str:
        status_map = {
            "idle": "простой",
            "sending": "отправка",
            "planning": "планирование",
            "tool_executing": "инструмент",
            "observing": "наблюдение",
            "verifying": "проверка",
            "waiting_approval": "одобрение",
            "done": "готово",
            "failed": "ошибка",
            "cancelled": "отменено",
            "reconnecting": "переподключение",
            "disconnected": "отключено",
            "timeout": "таймаут",
            "error": "ошибка",
        }
        label = status_map.get(self.state.current_status, self.state.current_status)
        return f"[{label}]"

    def _get_connection_text(self) -> str:
        conn_map = {
            "connected": "🟢 подключено",
            "reconnecting": "🔄 переподключение",
            "disconnected": "🔴 отключено",
        }
        return conn_map.get(self.state.connection, "⚪ неизвестно")

    def _header_extra(self) -> str:
        """Active flow + ``↓N новых`` badge (only while the viewport is frozen)."""
        parts: list[str] = []
        if self.state.active_flow_id:
            parts.append(f"задача {self.state.active_flow_id[:12]}")
        pending = self.new_message_count + self.new_events_indicator
        if pending > 0 and not self.auto_follow:
            parts.append(f"↓{pending} новых")
        return " · ".join(parts)

    def _create_center(self) -> Window:
        """Scrollable history; the only zone that resizes with the terminal."""
        return Window(
            FormattedTextControl(self._get_center_fragments),
            wrap_lines=True,
            height=Dimension(weight=1),
            right_margins=[ScrollbarMargin()],
        )

    def _create_status_line(self) -> Window:
        """Fixed 1-line pipeline bar: phase + last events + live activities."""
        return Window(
            FormattedTextControl(lambda: build_pipeline_bar(self.state)),
            height=1,
            style="class:status",
            dont_extend_height=True,
        )

    def _input_height(self) -> int:
        """Height reserved for the input box (3 lines for spacious multi-line input)."""
        return 3

    def _create_input(self) -> TextArea:
        """Spacious 3-line input area at the bottom of the screen."""
        ta = TextArea(
            prompt="> ",
            multiline=False,
            wrap_lines=True,
            width=None,
            height=self._input_height(),
            style=f"bg:{self.theme.menu_bg} fg:{self.theme.menu_fg}",
        )
        ta.window.height = self._input_height()
        return ta

    def _create_menu(self) -> Float:
        """``/`` command menu: a Float overlay above the status line.

        Overlaying (instead of reserving layout space) keeps the input line
        and the history in place — opening the menu never pushes anything and
        never triggers a full redraw.
        """
        return Float(
            content=Window(
                FormattedTextControl(self._get_menu_content),
                height=self._menu_height,
                wrap_lines=False,
                style="class:menu",
            ),
            left=1,
            bottom=1 + self._input_height(),
            width=self._menu_width,
        )

    def _create_layout(self) -> FloatContainer:
        self.header_window = self._create_header()
        self.portrait_window = self._create_portrait()
        self.center_window = self._create_center()
        self.status_window = self._create_status_line()
        self.input_window = self._create_input()
        self.menu_float = self._create_menu()

        body = HSplit([
            self.header_window,
            self.portrait_window,
            self.center_window,
            self.status_window,
            self.input_window,
        ])

        return FloatContainer(body, [self.menu_float])

    def _setup_application(self) -> None:
        """Build the prompt_toolkit Application (owns alternate screen)."""
        if self.container is None:
            self.container = self._create_layout()

        if self.input_window is not None:
            self.input_window.buffer.on_text_changed += (
                lambda _buf: self._on_input_text_changed()
            )

        self.application = Application(
            layout=Layout(self.container),
            key_bindings=self.kb,
            full_screen=True,
            mouse_support=True,
            style=self.app_style,
        )

    def _on_input_text_changed(self) -> None:
        """Synchronously handle text changes in the input buffer."""
        self._update_menu_visibility()
        self.request_repaint()

    # ── Scrolling (manual scroll mode) ───────────────────────────────────────

    def _content_width(self) -> int:
        cols, _rows = _terminal_size()
        return max(10, cols - 3)

    def _center_height(self) -> int:
        """History height after fixed header/portrait/status/input zones."""
        _cols, rows = _terminal_size()
        return max(1, rows - 4 - self._input_height() - self._portrait_height())

    def _flat_lines_with_role(self) -> list[tuple[str, str]]:
        """All history lines, pre-wrapped, paired with the owning message's role.

        Pure.  ``_flat_lines()`` (scroll math, plain-text tests) and
        ``_get_center_fragments()`` (coloured rendering) both derive from
        this single wrap pass, so the two never disagree on line breaks.
        """
        width = self._content_width()
        out: list[tuple[str, str]] = []
        for msg in self.state.messages:
            role = self._message_role(msg)
            for raw in (self._format_message(msg).splitlines() or [""]):
                out.extend((role, line) for line in _wrap_text(raw, width))
        return out

    def _flat_lines(self) -> list[str]:
        """All history lines, pre-wrapped to the content width (pure)."""
        return [line for _role, line in self._flat_lines_with_role()]

    def _center_window_bounds(self, total: int) -> tuple[int, int]:
        """Return (start, height) of the visible slice into the flat lines."""
        height = self._center_height()
        max_scroll = max(0, total - height)
        offset = 0 if self.auto_follow else min(self.scroll_offset, max_scroll)
        start = max(0, total - height - offset)
        return start, height

    def _get_center_content(self) -> str:
        """Visible history slice as plain text (pure; local clamp only).

        Kept alongside ``_get_center_fragments()`` (the actually-rendered,
        coloured version) because scroll math and existing tests key off
        plain substrings/line counts — converting this one to formatted
        text would turn every ``"m29" in content`` assertion into a false
        negative for no functional gain.
        """
        lines = self._flat_lines()
        if not lines:
            return "Добро пожаловать в Antigona CLI"
        start, height = self._center_window_bounds(len(lines))
        return "\n".join(lines[start : start + height])

    def _get_center_fragments(self) -> StyleAndTextTuples:
        """Visible history slice as coloured fragments (what the Window paints).

        Mirrors ``_get_center_content()``'s slicing exactly (same
        ``_flat_lines_with_role()`` source, same ``_center_window_bounds``),
        it just also carries each line's message-role colour.  Still a pure
        read: the role/colour was decided by the message that already
        arrived, not by a timer.
        """
        rows = self._flat_lines_with_role()
        if not rows:
            return [(f"fg:{ACCENT} bold", "✨ Добро пожаловать в Antigona CLI")]
        start, height = self._center_window_bounds(len(rows))
        visible = rows[start : start + height]
        last = len(visible) - 1
        fragments: StyleAndTextTuples = []
        for i, (role, line) in enumerate(visible):
            color = themes.role_color(self.theme, role)
            text = line if i == last else line + "\n"
            fragments.append((f"fg:{color}", text))
        return fragments

    def _message_role(self, msg: Any) -> str:
        """Normalized role string for a chat message (enum or plain str)."""
        role = getattr(msg, "role", "system")
        if hasattr(role, "value"):
            role = role.value
        return str(role)

    def _format_message(self, msg: Any) -> str:
        role_icons = {
            "user": "👤",
            "assistant": "🤖",
            "system": "⚙️",
            "tool": "🔧",
            "error": "❌",
            "warning": "⚠️",
            "info": "ℹ️",
        }
        role = self._message_role(msg)
        icon = role_icons.get(role, "💬")
        content = getattr(msg, "content", str(msg))
        timestamp = getattr(msg, "timestamp", "")
        if timestamp:
            return f"{icon} [{timestamp}] {content}"
        return f"{icon} {content}"

    def scroll_line_up(self) -> None:
        """Scroll one line up; freezes the viewport (manual mode)."""
        if not self.auto_follow and self.scroll_offset >= self.max_scroll:
            return
        self.auto_follow = False
        self.scroll_offset = min(self.scroll_offset + 1, self.max_scroll)
        self.request_repaint()

    def scroll_line_down(self) -> None:
        """Scroll one line down; reaching the bottom resumes auto-follow."""
        if self.scroll_offset <= 0:
            self.auto_follow = True
            self.request_repaint()
            return
        self.scroll_offset -= 1
        if self.scroll_offset == 0:
            self.auto_follow = True
            self.new_message_count = 0
        self.request_repaint()

    def scroll_page_up(self) -> None:
        if not self.auto_follow and self.scroll_offset >= self.max_scroll:
            return
        self.auto_follow = False
        self.scroll_offset = min(self.scroll_offset + self._center_height(), self.max_scroll)
        self.request_repaint()

    def scroll_page_down(self) -> None:
        if self.scroll_offset <= 0:
            self.auto_follow = True
            self.request_repaint()
            return
        self.scroll_offset = max(0, self.scroll_offset - self._center_height())
        if self.scroll_offset == 0:
            self.auto_follow = True
            self.new_message_count = 0
        self.request_repaint()

    def scroll_to_top(self) -> None:
        """Jump to the oldest history; freezes the viewport."""
        self.auto_follow = False
        self.scroll_offset = self.max_scroll
        self.request_repaint()

    def scroll_to_bottom(self) -> None:
        """Return to the live bottom: resume auto-follow, show accumulated updates."""
        self.auto_follow = True
        self.scroll_offset = 0
        self.new_message_count = 0
        self.request_repaint()

    # ── Refresh loop (bounded, deduplicated) ─────────────────────────────────

    def update_from_state(self) -> None:
        """Fold state deltas into layout indicators (called from the refresh loop)."""
        self._update_antigona_face_state()
        self._update_new_events_indicator()
        self._update_new_messages()
        self._clamp_scroll()
        self._update_portrait_animation()

    def _update_antigona_face_state(self) -> None:
        """Map the panel status to the face state (pure assignment)."""
        self.current_face_state = _STATUS_TO_FACE.get(self.state.current_status, "IDLE")

    def _update_new_events_indicator(self) -> None:
        """Count events that arrived while the viewport was frozen."""
        current = len(self.state.events)
        delta = max(0, current - self.last_event_count)
        if delta:
            if self.auto_follow:
                self.new_events_indicator = 0
            else:
                self.new_events_indicator += delta
            self.last_event_count = current

    def _update_new_messages(self) -> None:
        """Count messages that arrived while the viewport was frozen."""
        current = len(self.state.messages)
        if current > self._last_message_count:
            if not self.auto_follow:
                self.new_message_count += current - self._last_message_count
            self._last_message_count = current

    def _clamp_scroll(self) -> None:
        """Keep the scroll offset within the content bounds; resume follow at 0.

        ``max_scroll`` is recomputed on every tick (content/terminal size can
        change) even while auto-following, so a later scroll-up always has an
        up-to-date bound.
        """
        height = self._center_height()
        total = len(self._flat_lines())
        self.max_scroll = max(0, total - height)
        if self.auto_follow:
            self.scroll_offset = 0
            return
        self.scroll_offset = min(self.scroll_offset, self.max_scroll)
        if self.scroll_offset <= 0:
            self.auto_follow = True
            self.new_message_count = 0

    def _update_portrait_animation(self) -> None:
        """Advance portrait phases inside the ONE existing bounded refresh loop.

        No second timer/task is created. Active glitch/micro-expression rides on
        the same 4 Hz cadence already used by the pipeline spinner.
        """
        now = time.monotonic()
        dt = max(0.0, min(1.0, now - self._portrait_last_tick))
        self._portrait_last_tick = now
        self._portrait_phase = int(now * _SPINNER_TICK_HZ)

        # Gaze easing + Enter choreography ride on this same tick: the target is
        # recomputed from the (already pure) gaze getter, then eased towards.
        self._portrait_gaze()
        self.gaze_smoother.advance(dt)
        self._advance_submit_choreography(now, dt)

    def _animating_now(self) -> bool:
        """True while real work is in flight (drives the bounded spinner)."""
        return bool(
            self.state.is_animating
            and self.state.current_status not in _CALM_STATUSES
        ) or bool(get_tracker().list(live_only=True))

    def _refresh_interval(self) -> float:
        return _ACTIVE_REFRESH if (self._animating_now() or self.menu_visible) else _CALM_REFRESH

    def _spinner_tick(self) -> int:
        """Time-based frame index; constant 0 while calm so idle never repaints."""
        if self._animating_now():
            return int(time.monotonic() * _SPINNER_TICK_HZ)
        return 0

    def _render_key(self) -> tuple[Any, ...]:
        """Deterministic identity of everything the layout draws.

        Two ticks share a key only when the screen would be byte-identical, so
        ``invalidate()`` is skipped and the terminal stays untouched.
        """
        messages = self.state.messages
        last = messages[-1] if messages else None
        cols, rows = _terminal_size()
        events_tail = tuple(
            (str(e.get("event", "")), str(e.get("detail", "")))
            for e in self.state.events[-3:]
        )
        prof = self._portrait_profile()
        prof_name = prof.name if prof is not None else "hidden"
        return (
            len(messages),
            str(getattr(last, "role", "")),
            str(getattr(last, "content", "")),
            str(getattr(last, "timestamp", "")),
            self.state.current_status,
            self.state.connection,
            self.state.last_event,
            self.state.terminal_outcome is not None,
            events_tail,
            len(self.state.active_flows),
            len(self.state.pending_approvals),
            cols,
            rows,
            self.scroll_offset,
            self.auto_follow,
            self.new_message_count,
            self.new_events_indicator,
            self.menu_visible,
            self.menu_index,
            self._menu_prefix(),
            self._portrait_phase,
            prof_name,
            self._portrait_gaze_rendered(),
            self._portrait_expression(),
            self.portrait_current_phase,
            round(self.working_intensity, 2),
            self._portrait_input_focus(),
            self._spinner_tick(),
        )

    async def _refresh_loop(self) -> None:
        """Bounded refresh loop: fold state, invalidate only on real changes."""
        while True:
            try:
                self.update_from_state()
                self._update_menu_visibility()
                key = self._render_key()
                if key != self._last_render_key and self.application is not None:
                    self.application.invalidate()
                    self._last_render_key = key
            except Exception:
                # A broken tick must never kill the chat session.
                logger.exception("CLI layout refresh tick failed")
            await asyncio.sleep(self._refresh_interval())

    def request_repaint(self) -> None:
        """Invalidate the running application (no-op before it starts).

        Public entry point used by the LayoutRendererAdapter and scroll/menu
        handlers; the refresh loop uses ``application.invalidate()`` directly.
        """
        if self.application is not None:
            self.application.invalidate()

    def _update_center_content(self) -> None:
        """Legacy alias: repaint the history zone through the running app."""
        self.request_repaint()

    # ── Slash menu ───────────────────────────────────────────────────────────

    def _menu_prefix(self) -> str:
        """The typed command prefix after ``/`` (pure read)."""
        buf = self._current_buffer()
        if buf is None:
            return ""
        text = buf.text or ""
        if not text.startswith("/") or " " in text:
            return ""
        return text[1:].lower()

    def _menu_commands(self, prefix: str | None = None) -> list[SlashCommand]:
        """Commands matching the typed prefix (pure read; [] → menu hidden)."""
        if prefix is None:
            prefix = self._menu_prefix()
        needle = "/" + prefix
        return [c for c in self.catalog if c.name.lower().startswith(needle)]

    def _current_buffer(self) -> Any:
        """The application's focused buffer, or None (defensive)."""
        if self.application is None:
            return None
        try:
            return self.application.current_buffer
        except Exception:
            return None

    def _is_menu_active(self) -> bool:
        """Evaluate menu visibility synchronously from the current buffer text."""
        self._update_menu_visibility()
        return self.menu_visible

    def _update_menu_visibility(self) -> None:
        """Show the menu while the buffer starts with ``/``; hide otherwise."""
        buf = self._current_buffer()
        text = buf.text if buf is not None else ""
        prefix = text[1:].lower() if text.startswith("/") else ""
        show = (
            text.startswith("/")
            and " " not in text
            and bool(self._menu_commands(prefix))
        )
        if show != self.menu_visible:
            self.menu_visible = show
            self.menu_index = 0
        elif show:
            commands = self._menu_commands(prefix)
            if commands:
                self.menu_index %= len(commands)

    def _menu_width(self) -> int:
        cols, _rows = _terminal_size()
        return max(10, min(cols - 4, 64))

    def _menu_height(self) -> int:
        if not self.menu_visible:
            return 0
        commands = self._menu_commands()
        if not commands:
            return 0
        _cols, rows = _terminal_size()
        selected = commands[self.menu_index % len(commands)]
        help_lines = build_command_help(selected, self._menu_width()).count("\n") + 1
        items = min(len(commands), _MAX_MENU_ITEMS)
        return max(1, min(3 + items + help_lines + 1, max(1, rows - 6)))

    def _get_menu_content(self) -> str:
        """Menu overlay content: command list + help card for the selection."""
        if not self.menu_visible:
            return ""
        commands = self._menu_commands()
        if not commands:
            return ""
        width = self._menu_width()

        lines: list[str] = []
        title = f" ⚡ Команды ({len(commands)}) "
        lines.append("╭─" + title + "─" * max(0, width - 2 - len(title)) + "╮")
        for idx, cmd in enumerate(commands[:_MAX_MENU_ITEMS]):
            marker = "▶" if idx == self.menu_index else " "
            desc = f"{cmd.emoji} {cmd.description}".strip()
            row = f"{marker} {cmd.name}  {desc}"
            lines.append("│ " + _pad_cells(row, width - 4) + " │")
        if len(commands) > _MAX_MENU_ITEMS:
            lines.append("│ " + _pad_cells(f"… ещё {len(commands) - _MAX_MENU_ITEMS}", width - 4) + " │")
        lines.append("╰" + "─" * (width - 2) + "╯")

        selected = commands[self.menu_index]
        lines.append("")
        lines.extend(build_command_help(selected, width).splitlines())
        lines.append("")
        lines.append("↑↓ выбор · Enter выполнить/вставить · Tab далее · Esc закрыть")
        return "\n".join(lines)

    def _menu_move(self, delta: int) -> None:
        commands = self._menu_commands()
        if commands:
            self.menu_index = (self.menu_index + delta) % len(commands)
            self.request_repaint()

    def _menu_activate(self, event: Any) -> None:
        """Enter on a selected command: insert (needs args) or execute directly."""
        commands = self._menu_commands()
        if not commands:
            self.menu_visible = False
            return
        cmd = commands[self.menu_index % len(commands)]
        if cmd.usage and cmd.usage.strip() != cmd.name:
            # Command takes arguments — insert its name so the user can type them.
            self._set_input_text(cmd.name + " ")
            return
        self.menu_visible = False
        self._set_input_text("")
        event.app.create_background_task(self.on_input(cmd.name))

    def _menu_complete(self, event: Any) -> None:
        """Complete/insert the selected command name into the input buffer."""
        commands = self._menu_commands()
        if not commands:
            self.menu_visible = False
            return
        cmd = commands[self.menu_index % len(commands)]
        self._set_input_text(cmd.name + " ")
        self.menu_visible = False
        self.request_repaint()

    def _set_input_text(self, text: str) -> None:
        buf = self._current_buffer()
        if buf is None:
            return
        buf.text = text
        buf.cursor_position = len(text)


    def _history_previous(self, event: Any) -> None:
        """Arrow up: replace the input line with the previous submitted message.

        Uses Antigona's own in-memory recall history (prompt_toolkit's
        ``Buffer._working_lines`` snapshot is not refreshed for a live-growing
        history in a full-screen app, so its built-in navigation would be a
        no-op here).
        """
        if not self._input_history:
            return
        buf = self._current_buffer()
        if self._history_pos is None:
            self._working_line = buf.text if buf is not None else ""
            self._history_pos = len(self._input_history) - 1
        elif self._history_pos > 0:
            self._history_pos -= 1
        else:
            return
        self._set_input_text(self._input_history[self._history_pos])

    def _history_next(self, event: Any) -> None:
        """Arrow down: move forward through recall history, back to the live line."""
        if self._history_pos is None:
            return
        self._history_pos += 1
        if self._history_pos >= len(self._input_history):
            self._history_pos = None
            self._set_input_text(self._working_line)
        else:
            self._set_input_text(self._input_history[self._history_pos])

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def apply_theme(self, name: str) -> bool:
        """Activate a named colour theme and repaint the layout live.

        Returns False (without changing anything) when the theme is unknown.
        Persists via ``themes.set_active`` so the choice survives restart.
        """
        try:
            self.theme = themes.set_active(name)
        except KeyError:
            return False
        self.app_style = themes.build_style(self.theme)
        if self.application is not None:
            self.application.style = self.app_style
        self.request_repaint()
        return True

    def apply_custom_theme(self, slots: dict[str, str]) -> bool:
        """Build, activate and repaint a custom theme from slot overrides.

        *slots* maps theme colour slots (see ``themes.CUSTOM_SLOT_ORDER``) to
        hex values. Invalid values fall back to the aurora default; the theme
        is persisted via ``themes.set_custom``.
        """
        self.theme = themes.set_custom(slots)
        self.app_style = themes.build_style(self.theme)
        if self.application is not None:
            self.application.style = self.app_style
        self.request_repaint()
        return True

    def _handle_submit(self, text: str) -> None:
        """Route one submitted input line: /exit closes the app, else dispatch.

        Extracted from the accept_handler closure so it is unit-testable
        without a real prompt_toolkit ``Application``/``Buffer``.

        /exit and /quit must stop the application itself, not just report a
        disposition nobody reads: ``on_input()`` is fired as a detached
        background task and its return value is never inspected, so a bare
        ``CommandDisposition`` from ``handle_input()`` cannot close the TUI.
        Intercepted here, before dispatch — mirrors what the plain
        (non-full-screen) input loop already does.
        """
        stripped = text.strip()
        if not stripped:
            return
        # Hold the gaze where it was, glide back to centre, then start working.
        self.begin_submit_choreography()
        # Push into in-memory recall history (Up/Down arrows). Capped so a long
        # session cannot grow memory without bound.
        self._input_history.append(text)
        if len(self._input_history) > _MAX_INPUT_HISTORY:
            del self._input_history[0]
        self._history_pos = None
        self._working_line = ""
        if parse_command(text).kind == CommandKind.EXIT:
            if self.application is not None:
                self.application.exit()
            return
        result = self.on_input(text)
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)

    async def run(self) -> None:
        """Start the full-screen application (after optional owner PIN gate)."""
        if not self.owner_mode:
            await self._request_owner_pin()

        self._setup_application()

        if self.application:
            def accept_handler(buffer: Any) -> bool:
                self._handle_submit(buffer.text)
                buffer.reset()
                return True

            if self.input_window:
                self.input_window.accept_handler = accept_handler

            self._refresh_task = asyncio.create_task(self._refresh_loop())
            try:
                await self.application.run_async()
            finally:
                if self._refresh_task is not None:
                    self._refresh_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._refresh_task
                    self._refresh_task = None

    def _grant_owner_mode(self) -> None:
        """Enter owner mode AND record the elevation in the shared authority.

        ``owner_mode`` alone is a per-process flag; recording it with
        :func:`owner_elevation_authority` is what lets the canonical tool path
        (and ``/lock`` from any surface) see the CLI's PIN state instead of the
        CLI self-certifying it (campaign G-02b / CP-2).
        """
        self.owner_mode = True
        try:
            from antigona.security.elevation import (
                CLI_OWNER_PRINCIPAL,
                owner_elevation_authority,
            )

            owner_elevation_authority().elevate(CLI_OWNER_PRINCIPAL)
        except Exception:  # elevation store is best-effort here; never block the CLI
            logger.debug("CLI owner-mode elevation not recorded", exc_info=True)

    async def _request_owner_pin(self) -> None:
        """Request PIN for Owner Mode authentication.

        This prompt runs before the full-screen Application starts, so
        prompt_toolkit renders it as a plain (non full-screen) prompt, which
        probes cursor position (CPR) to size itself.  Many PTYs (Termux, some
        multiplexers/CI sandboxes) never answer that probe, so prompt_toolkit
        waits out its timeout and prints "your terminal doesn't support cursor
        position requests" straight into the PIN screen.  The PIN prompt is one
        fixed-height password line, so the probe is disabled at the source.
        """
        os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")

        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import DummyHistory
        from prompt_toolkit.shortcuts import PromptSession

        # History strictly disabled — the PIN must never be persisted.
        session: PromptSession[str] = PromptSession(history=DummyHistory())

        if os.environ.get("ANTIGONA_SKIP_PIN") in {"1", "true", "TRUE", "yes"}:
            self._grant_owner_mode()
            return

        while self.pin_attempts < self.max_pin_attempts:
            try:
                pin_prompt = HTML('<style bg="#7C3AED" fg="#ffffff"> 🔐 Введите PIN: </style>')
                # is_password=True masks typed digits as **** (no plaintext leak).
                pin_input = await read_prompt(
                    prompt_str=pin_prompt, session=session, is_password=True
                )

                import hashlib
                import hmac

                pin = os.environ.get("ANTIGONA_PIN", "")
                if not pin:
                    # No PIN configured → development access.
                    self._grant_owner_mode()
                    return

                expected = hashlib.sha256(pin.encode()).hexdigest()
                actual = hashlib.sha256(pin_input.encode()).hexdigest()

                if hmac.compare_digest(expected, actual):
                    self._grant_owner_mode()
                    self.pin_attempts = 0
                    return

                self.pin_attempts += 1
                remaining = self.max_pin_attempts - self.pin_attempts
                if remaining > 0:
                    error_prompt = HTML(
                        f'<style bg="#FF6E6E" fg="#ffffff"> ❌ Неверный PIN. '
                        f"Осталось попыток: {remaining} </style>"
                    )
                    await read_prompt(prompt_str=error_prompt, session=session)
                else:
                    error_prompt = HTML(
                        '<style bg="#FF6E6E" fg="#ffffff"> ❌ Превышено количество '
                        "попыток. Доступ запрещен. </style>"
                    )
                    await read_prompt(prompt_str=error_prompt, session=session)
                    return
            except (KeyboardInterrupt, EOFError):
                # User aborted (Ctrl+C / EOF) — deny access without killing the process.
                return

    def stop(self) -> None:
        """Stop the layout application."""
        if self.application is not None:
            with contextlib.suppress(Exception):
                self.application.exit()
