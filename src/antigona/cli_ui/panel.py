"""Live status panel for the Antigona CLI chat (mobile-first, no full-screen TUI).

Builds the always-visible CLI dashboard: a colour banner + a compact panel fed
by REAL Gateway data (flows, approvals, status) plus a bounded, safe event feed
from the Gateway EventLog.  The panel is static between state changes and is
redrawn in place only when something actually changed — it never grabs the
screen, never runs heavy Live/full-screen loops, and works on 40–60 col
terminals (Termux) as well as wide ones.

Emoji are used as visual markers; when the terminal cannot render them, an
ASCII fallback (``[OK]``/``[ERR]``/``[WAIT]``/``[RUN]``) is used instead.
"""

from __future__ import annotations

from math import sin
from typing import Any, Final

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from antigona.cli_ui.models import ChatUIState, TerminalOutcomeStatus

# AURORA palette (mirrors cli_ui/status_bar.py aesthetic).
ACCENT = "#56E2A0"
VIOLET = "#7C3AED"
MAGENTA = "#E83EDC"
AMBER = "#FFC857"
MUTED_GREY = "#5A5878"
MUTED_LAVENDER = "#7C7A96"
BLUE = "#6EA0FF"
RED = "#FF6E6E"

#: Banner letters with their gradient colours.
BANNER_COLORS: tuple[tuple[str, str], ...] = (
    ("A", ACCENT),
    ("N", "#6EFFBE"),
    ("T", "#6EDCFF"),
    ("I", VIOLET),
    ("G", MAGENTA),
    ("O", "#FFA050"),
    ("N", ACCENT),
    ("A", "#6EFFBE"),
)

#: Gradient stops for the underline.
_UNDERLINE_GRADIENT: tuple[tuple[int, int, int], ...] = (
    (40, 90, 200),
    (90, 60, 200),
    (140, 50, 190),
    (190, 45, 170),
    (240, 60, 140),
    (255, 120, 90),
)

# ── States: (marker_emoji, ascii, rich colour, human label) ──────────────────
#: Panels support explicit states; markers double as status dots.
STATE_META: Final[dict[str, tuple[str, str, str, str]]] = {
    "idle": ("⚪", "[--]", MUTED_LAVENDER, "idle"),
    "sending": ("📨", "[>>]", BLUE, "отправка"),
    "planning": ("🧠", "[PLAN]", VIOLET, "планирование"),
    "waiting_approval": ("🔐", "[WAIT]", AMBER, "ждёт одобрения"),
    "tool_executing": ("🛠", "[RUN]", ACCENT, "инструмент"),
    "observing": ("👁", "[OBS]", BLUE, "наблюдение"),
    "verifying": ("🛡", "[VER]", MAGENTA, "проверка"),
    "done": ("✅", "[OK]", ACCENT, "готово"),
    "failed": ("❌", "[ERR]", RED, "ошибка"),
    "cancelled": ("⛔", "[CAN]", RED, "отменено"),
    "reconnecting": ("🔄", "[RETRY]", AMBER, "переподключение"),
    "disconnected": ("🔴", "[OFF]", RED, "нет соединения"),
    "timeout": ("❌", "[TO]", RED, "таймаут"),
    "error": ("❌", "[ERR]", RED, "ошибка"),
}

#: Emoji marker for event types (safe, structured only).
EVENT_EMOJI: Final[dict[str, str]] = {
    "plan": "🧠",
    "planning": "🧠",
    "analysis": "🔎",
    "analyze": "🔎",
    "tool": "🛠",
    "tool_executing": "🛠",
    "approval": "🔐",
    "approval_created": "🔐",
    "approved": "✅",
    "denied": "❌",
    "artifact": "📄",
    "artifact_created": "📄",
    "verifier": "🛡",
    "verifying": "🛡",
    "verified": "✅",
    "done": "✅",
    "failed": "❌",
    "error": "❌",
    "retry": "🔁",
    "cancelled": "⛔",
    "cancel": "⛔",
    "state": "📌",
    "flow": "📌",
    "session": "💬",
    "memory": "🧠",
    "steer": "🧭",
    "reconnecting": "🔄",
    "connected": "🟢",
}

#: Bounded panel body width for wide terminals.
_PANEL_BODY_WIDTH = 78
#: Narrow-terminal body width (mobile / Termux).
_NARROW_BODY_WIDTH = 38

#: Spinner frames (calm, ~8 FPS).
SPINNER_FRAMES: Final[str] = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def use_emoji(console: Console) -> bool:
    """Whether the console can render emoji (fallback to ASCII otherwise)."""
    if console.no_color:
        return False
    legacy = getattr(console, "legacy_windows", False)
    return not legacy


def _state_meta(status: str) -> tuple[str, str, str, str]:
    return STATE_META.get(
        status, ("⚪", "[--]", MUTED_LAVENDER, status or "idle")
    )


def marker(status: str, emoji: bool) -> str:
    """Return the status marker (emoji or ASCII fallback)."""
    meta = _state_meta(status)
    return meta[0] if emoji else meta[1]


def _truncate(text: str, max_width: int) -> str:
    """Truncate text to *max_width* chars, appending ``…`` when cut."""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= max_width:
        return text
    return text[: max_width - 1] + "…"


def _short_id(value: str, keep: int = 8) -> str:
    """Shorten a long ID for narrow screens (``84c1a2f3…``)."""
    value = (value or "").strip()
    if len(value) <= keep + 1:
        return value
    return f"{value[:keep]}…"


#: Wide ASCII-art banner (spec §3) — used on terminals wider than 50 cols.
_BANNER_ART: Final[tuple[str, ...]] = (
    "     █████╗ ███╗   ██╗████████╗██╗ ██████╗  ██████╗ ███╗   ██╗ █████╗",
    "    ██╔══██╗████╗  ██║╚══██╔══╝██║██╔════╝ ██╔═══██╗████╗  ██║██╔══██╗",
    "    ███████║██╔██╗ ██║   ██║   ██║██║  ███╗██║   ██║██╔██╗ ██║███████║",
    "    ██╔══██║██║╚██╗██║   ██║   ██║██║   ██║██║   ██║██║╚██╗██║██╔══██║",
    "    ██║  ██║██║ ╚████║   ██║   ██║╚██████╔╝╚██████╔╝██║ ╚████║██║  ██║",
    "    ╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝   ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝╚═╝  ╚═╝",
)

#: Compact Termux banner (spec §3) for terminals <= 50 cols.
def _banner_compact(width: int) -> tuple[str, ...]:
    """Build the compact Termux banner box adapted to the terminal width.

    Guarantees no line is wider than *width* (spec §3: «ни одна строка не
    шире терминала»).  Paddings account for wide characters (emoji ⚡).
    """
    from rich.cells import cell_len

    inner = max(width - 2, 24)
    top = "╭" + "─" * inner + "╮"
    bottom = "╰" + "─" * inner + "╯"
    title = "⚡ ANTIGONA"
    sub = "Durable Agent Control Center"
    title_w = cell_len(title)
    sub_w = cell_len(sub)

    def _row(text: str, text_w: int) -> str:
        pad = max((inner - text_w) // 2, 0)
        tail = max(inner - pad - text_w, 0)
        return "│" + " " * pad + text + " " * tail + "│"

    return (
        top,
        _row(title, title_w),
        _row(sub, sub_w),
        bottom,
    )


def banner_text(no_color: bool = False, width: int | None = None) -> str:
    """Return the plain banner text (ASCII fallback for colour-less terminals).

    *width* selects the compact Termux variant for narrow terminals.
    """
    if width is not None and width <= _BANNER_COMPACT_MAX_WIDTH:
        if no_color:
            return "ANTIGONA — Durable Agent Control Center"
        return "\n".join(_banner_compact(width))
    if no_color:
        return "\n".join(_BANNER_ART) + "\nANTIGONA — Durable Agent Control Center"
    return "\n".join(_BANNER_ART) + "\n⚡ Durable Agent Control Center"

#: Banner line colours (wide art): gradient across the logo rows.
_BANNER_LINE_COLORS: Final[tuple[str, ...]] = (
    "#A78BFA",
    "#7C3AED",
    "#E83EDC",
    "#56E2A0",
    "#38BDF8",
    "#5B8DEF",
)

#: Wide art needs ~62 columns; below this use the adaptive compact box.
_BANNER_COMPACT_MAX_WIDTH: Final[int] = 66


def _banner_compact_text(width: int) -> str:
    return "\n".join(_banner_compact(width))


def render_banner(console: Console) -> None:
    """Print the colour banner to *console* (wide art or compact Termux box).

    Falls back to plain text on colour-less terminals.  The banner is printed
    once at session start and never redrawn.
    """
    width = console.width
    if console.no_color:
        console.print(banner_text(no_color=True, width=width))
        console.print("=" * min(width, _NARROW_BODY_WIDTH + 4))
        return

    if width <= _BANNER_COMPACT_MAX_WIDTH:
        # Compact Termux variant: centred box, amber accent.
        t = Text()
        for line in _banner_compact(width):
            if "ANTIGONA" in line or "Control Center" in line:
                t.append(line, style=f"bold {AMBER}")
            else:
                t.append(line, style=MUTED_LAVENDER)
            t.append("\n")
        console.print(t, end="")
        return

    emoji = use_emoji(console)
    # Portrait art: half-block ANSI render of assets/antigona_banner.png when
    # available (visual effect — our Antigona image, not just the ASCII logo).
    art: list[str] = []
    # Portrait art is a visual effect for interactive terminals; in non-TTY
    # streams (pipes, tests) we keep the compact ASCII banner.
    if console.is_terminal and not console.no_color:
        try:
            from antigona.cli_ui.art import art_lines as _art_lines

            # Subtle breathing pulse: brighten while the agent is working.
            _breathe = 0.0
            try:
                from antigona.cli_ui.activity import get_tracker

                if get_tracker().count() > 0:
                    import time as _time

                    _breathe = (1.0 + (sin(_time.monotonic() * 2.2))) / 2
            except Exception:  # pragma: no cover - tracker optional
                pass
            art = _art_lines(width, breathe=_breathe)
        except Exception:  # pragma: no cover - optional dependency
            art = []
    if art:
        # Write half-block art directly to the stream: Rich would re-wrap
        # the long ANSI sequences and explode the line count.
        _stream = console.file
        _stream.write("\n")
        for line in art:
            _stream.write(line + "\n")
        _stream.write("\n")
        try:
            _stream.flush()
        except Exception:  # pragma: no cover
            pass
    t = Text()
    for i, line in enumerate(_BANNER_ART):
        color = _BANNER_LINE_COLORS[i % len(_BANNER_LINE_COLORS)]
        t.append(line, style=f"bold {color}")
        t.append("\n")
    tag = "⚡ Durable Agent Control Center" if emoji else "* Durable Agent Control Center"
    t.append(tag, style=f"bold {AMBER}")
    console.print(t)


# ── Data row helpers ─────────────────────────────────────────────────────────


def _flow_row(flow: dict[str, Any] | Any, width: int) -> str:
    """One compact flow line for the panel body."""
    if isinstance(flow, dict):
        flow_id = str(flow.get("flow_id") or flow.get("id") or "?")
        status = str(flow.get("status") or "UNKNOWN")
        title = str(flow.get("title") or flow.get("goal") or "")
    else:
        flow_id = str(getattr(flow, "flow_id", getattr(flow, "id", "?")))
        status = str(getattr(flow, "status", "UNKNOWN"))
        title = str(getattr(flow, "title", getattr(flow, "goal", "")))
    row = f"{_short_id(flow_id)}  {status}"
    if title and width > _NARROW_BODY_WIDTH:
        row += f"  ·  {title}"
    return _truncate(row, width)


def _approval_row(app: dict[str, Any] | Any, width: int) -> str:
    """One compact approval line for the panel body."""
    if isinstance(app, dict):
        app_id = str(app.get("approval_id") or app.get("id") or "?")
        reason = str(app.get("reason") or app.get("description") or app.get("tool") or "")
    else:
        app_id = str(getattr(app, "approval_id", getattr(app, "id", "?")))
        reason = str(
            getattr(app, "reason", "")
            or getattr(app, "description", "")
            or getattr(app, "tool", "")
        )
    return _truncate(f"{_short_id(app_id, 8)}  {reason}", width)


def _safe_event(event: dict[str, Any] | Any) -> dict[str, str]:
    """Project a Gateway event into a safe, display-only shape.

    Only structured fields are forwarded; free-form payloads, provider
    responses, reasoning, secrets and raw tool arguments are dropped.
    """
    if isinstance(event, dict):
        raw = event
    else:
        raw = {}
        for attr in ("event", "type", "status", "flow_id", "detail", "stage", "tool"):
            value = getattr(event, attr, None)
            if value is not None:
                raw[attr] = value
    out: dict[str, str] = {}
    etype = str(raw.get("event") or raw.get("type") or raw.get("stage") or "")
    detail = str(raw.get("detail") or raw.get("tool") or raw.get("description") or "")
    status = str(raw.get("status") or "")
    flow_id = str(raw.get("flow_id") or "")
    out["event"] = _truncate(etype, 20)
    out["detail"] = _truncate(detail, 28)
    out["status"] = _truncate(status, 10)
    if flow_id and flow_id != "None":
        out["flow_id"] = _short_id(flow_id, 8)
    return out


def _event_line(event: dict[str, Any] | Any, emoji: bool, width: int) -> str:
    """One safe event line: marker + type + short detail (no secrets)."""
    safe = _safe_event(event)
    etype = safe.get("event", "")
    detail = safe.get("detail", "")
    icon = EVENT_EMOJI.get(etype.lower(), "•")
    if not emoji:
        icon = "·"
    parts = [icon, " ", etype]
    if detail:
        parts.append(" — ")
        parts.append(detail)
    # Emoji are 2 cells wide; keep a 2-cell safety margin inside the box.
    return _truncate("".join(parts), max(1, width - 2))


def _status_line(state: ChatUIState, width: int, emoji: bool) -> str:
    """Return the 'Status' line content for the panel."""
    status = state.current_status or "idle"
    mk = marker(status, emoji)
    _, _, color, label = _state_meta(status)
    flows = len(state.active_flows)
    approvals = len(state.pending_approvals)
    if width <= _NARROW_BODY_WIDTH + 12:  # compact layout up to ~50 cols
        return f"[{color}]{mk}[/] {label}  f:{flows} a:{approvals}"
    return f"[{color}]{mk}[/] {label}   флоу: {flows}   одобрений: {approvals}"


def _connection_line(state: ChatUIState, width: int, emoji: bool) -> str:
    """One line about Gateway connection + session (always the first row)."""
    conn = state.connection or "connected"
    if conn == "reconnecting":
        icon, color = ("🔄", AMBER) if emoji else ("[RETRY]", AMBER)
        label = "переподключение…"
    elif conn == "disconnected":
        icon, color = ("🔴", RED) if emoji else ("[OFF]", RED)
        label = "Gateway отключён"
    else:
        icon, color = ("🟢", ACCENT) if emoji else ("[OK]", ACCENT)
        label = "Gateway connected"
    session = _short_id(state.session_id, 8)
    host = _short_id(state.gateway_url.replace("http://", "").replace("https://", ""), 18)
    if width <= _NARROW_BODY_WIDTH:
        tail = f" 💬 {session}" if emoji else f" session {session}"
        return _truncate(f"[{color}]{icon}[/] {label}{tail}", width)
    return _truncate(
        f"[{color}]{icon}[/] {label} ({host})"
        f"   💬 Session: {_short_id(state.session_id, 16)}",
        width,
    )


def _outcome_suffix(state: ChatUIState) -> str:
    """Append a compact terminal-outcome note when one is present."""
    outcome = state.terminal_outcome
    if outcome is None:
        return ""
    status = outcome.status
    if isinstance(status, TerminalOutcomeStatus):
        label = status.value
    else:
        label = str(status)
    if outcome.is_success():
        return f" · {label}"
    return f" · {label}"


# ── Panel assembly ───────────────────────────────────────────────────────────


def panel_body_lines(state: ChatUIState, width: int, emoji: bool = True) -> list[str]:
    """Build the (bounded) body lines for the panel at the given width."""
    lines: list[str] = []

    lines.append(_connection_line(state, width, emoji))
    lines.append(_status_line(state, width, emoji) + _outcome_suffix(state))

    flow = state.active_flows[0] if state.active_flows else None
    if flow is not None:
        lines.append(f"📌 {_flow_row(flow, width)}" if emoji else f"flow: {_flow_row(flow, width)}")

    if state.pending_approvals:
        lines.append(f"🔐 {_approval_row(state.pending_approvals[0], width)}" if emoji
                     else f"approval: {_approval_row(state.pending_approvals[0], width)}")

    # Live event feed: last 3 events on narrow, 5 on wide (spec §6).
    limit = 3 if width <= _NARROW_BODY_WIDTH else 5
    events = state.events[-limit:]
    for event in events:
        lines.append(_event_line(event, emoji, width))

    if not state.pending_approvals and not events:
        if state.last_event:
            lines.append(f"посл.: {_truncate(state.last_event, width)}")
        else:
            lines.append("ожидание…" if emoji else "waiting...")

    return lines


def build_panel_console(state: ChatUIState, console: Console) -> Panel:
    """Build the Rich Panel for the dashboard at the console width."""
    width = min(console.width or _NARROW_BODY_WIDTH + 4, _PANEL_BODY_WIDTH + 4)
    body_width = width - 4
    emoji = use_emoji(console)
    lines = panel_body_lines(state, body_width, emoji)
    body = "\n".join(lines)
    return Panel(
        body,
        title="ПУЛЬТ",
        border_style="bold #464464",
        title_align="left",
        expand=False,
        width=width,
    )


def _banner_height(width: int) -> int:
    """Number of terminal lines the banner occupies at the given width."""
    if width <= _BANNER_COMPACT_MAX_WIDTH:
        return 4  # compact box (4 lines)
    return len(_BANNER_ART) + 1  # wide art (6 lines) + tag line


def panel_height(state: ChatUIState, width: int, emoji: bool = True) -> int:
    """Return the number of terminal lines the panel block occupies."""
    body = len(panel_body_lines(state, width, emoji))
    return _banner_height(width) + (body + 2) + 1  # banner + box(borders) + trailing


def render_panel(console: Console, state: ChatUIState) -> None:
    """Render banner + dashboard panel to *console* (one static redraw)."""
    render_banner(console)
    console.print(build_panel_console(state, console))
    console.print()


__all__ = [
    "ACCENT",
    "AMBER",
    "BLUE",
    "EVENT_EMOJI",
    "MAGENTA",
    "MUTED_GREY",
    "MUTED_LAVENDER",
    "RED",
    "SPINNER_FRAMES",
    "STATE_META",
    "VIOLET",
    "banner_text",
    "build_panel_console",
    "marker",
    "panel_body_lines",
    "panel_height",
    "render_banner",
    "render_panel",
    "use_emoji",
]
