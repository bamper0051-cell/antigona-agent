"""Unified animated Antigona Console — live chat + agent activity monitor.

A single Textual console that gives the owner full visibility into the live
Antigona core over the canonical Gateway HTTP/WS surface:

  Chat       — live chat with the agent (turn API). Free text is a *turn*
               through the server brain; the returned response_type decides
               rendering (conversation reply / task_accepted live-wait /
               clarification). The agent can act as **orchestrator** (routes,
               delegates, steers) or **executor** (runs tools directly) — both
               roles are surfaced as badges and steered via the same turn API.
  Activity   — real-time monitor of *what the agent is doing right now*:
               flow transitions, tool progress, approvals. Polls the Gateway
               events feed and re-renders an animated live table.
  Flows      — list of the owner's flows (GET /flows).
  Approvals  — pending approvals with approve/reject buttons.

The bottom status bar animates a spinner + live activity count + connection /
Telegram-bot state, and re-renders on a timer (no blocking).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:
    print(
        "\n❌ Не запускайте этот файл напрямую "
        "(`python tui_console.py`) — так src/antigona/ попадает в sys.path "
        "и конфликтует со стандартным модулем `queue`, "
        "из-за чего импорт валится с неочевидной ошибкой.\n\n"
        "Используйте штатный вход:\n"
        "  antigona panel                    (после `uv sync` / `pip install -e .`)\n"
        "  python -m antigona.tui_console    (запуск из корня проекта без установки)\n"
    )
    raise SystemExit(1)

import asyncio
import uuid
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    Static,
    Tab,
    Tabs,
)

from antigona.core.gateway_client import GatewayClient as CoreGatewayClient

__all__ = ["AntigonaConsole", "main"]

# ── Tab topology ──────────────────────────────────────────────────────
TAB_SPECS: tuple[tuple[str, str, str], ...] = (
    ("tab-chat", "Chat", "pane-chat"),
    ("tab-activity", "Activity", "pane-activity"),
    ("tab-flows", "Flows", "pane-flows"),
    ("tab-approvals", "Approvals", "pane-approvals"),
)
MAIN_PANES: dict[str, str] = {tab: pane for tab, _t, pane in TAB_SPECS}

EVENT_COLUMNS = ("#", "Event", "Flow", "Detail")
FLOW_COLUMNS = ("ID", "Goal", "Status", "Rev", "Created")
APPROVAL_COLUMNS = ("ID", "Flow", "Tool", "Risk", "Reason")

# Palette (matches the CLI status-bar AURORA family).
ACCENT = "#7C3AED"
MAGENTA = "#E83EDC"
MINT = "#56E2A0"
MUTED = "#5A5878"
BG = "#1a1b26"

STATUS_STYLES: dict[str, str] = {
    "done": "green", "failed": "red", "blocked": "red",
    "policy_denied": "red", "waiting_approval": "yellow",
    "cancelled": "grey50", "timeout": "grey50",
    "received": "blue", "queued": "blue", "planning": "blue",
    "running": "blue", "tool_executing": "blue", "observing": "blue",
    "verifying": "blue",
}
RISK_STYLES: dict[str, str] = {"high": "red", "medium": "yellow", "low": "grey50"}


def status_color(status: str) -> str:
    return STATUS_STYLES.get((status or "").strip().lower(), "white")


def risk_color(risk: str) -> str:
    return RISK_STYLES.get((risk or "").strip().lower(), "white")


def _clip(value: Any, limit: int = 50) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stamp(value: Any) -> str:
    return str(value or "")[:19].replace("T", " ")


def _event_row(index: int, ev: dict[str, Any]) -> tuple[str, str, str, str]:
    ev_type = str(ev.get("type", ev.get("event_type", "")))
    flow = str(ev.get("flow_id", ev.get("task_id", "")))[:8]
    payload: Any = ev.get("payload") or {}
    if isinstance(payload, dict):
        detail = _clip(payload.get("to_state") or payload.get("status")
                       or payload.get("tool_name") or payload.get("message") or "", 40)
    else:
        detail = _clip(payload, 40)
    return str(index), ev_type, flow or "-", detail


def _flow_dict(f: Any) -> dict[str, Any]:
    if isinstance(f, dict):
        return f
    return {
        "id": getattr(f, "flow_id", "?"),
        "goal": getattr(f, "title", getattr(f, "goal", "")),
        "status": getattr(getattr(f, "status", None), "value", None)
        or getattr(f, "status", "?"),
        "revision": str(getattr(f, "revision", "") or ""),
        "created_at": getattr(f, "created_at", ""),
    }


# ── Animated status bar ──────────────────────────────────────────────
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusBar(Static):
    """Bottom status bar: animated spinner + connection + telegram + activity."""

    connected: reactive[bool] = reactive(False)
    telegram: reactive[str] = reactive("unknown")
    role: reactive[str] = reactive("orchestrator")
    activity: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tick = 0

    def watch_connected(self, _v: bool) -> None:
        self._redraw()

    def watch_telegram(self, _v: str) -> None:
        self._redraw()

    def watch_role(self, _v: str) -> None:
        self._redraw()

    def watch_activity(self, _v: int) -> None:
        self._redraw()

    def animate_tick(self) -> None:
        """Advance one animation frame and redraw (driven by app interval)."""
        self._tick += 1
        self._redraw()

    def _redraw(self) -> None:
        spin = _SPINNER[self._tick % len(_SPINNER)]
        conn = "●" if self.connected else "○"
        conn_style = "green" if self.connected else "red"
        tg = {"running": "●", "stopped": "○", "unknown": "?"}
        tg_char = tg.get(self.telegram.lower(), "?")
        parts = [
            f"[{ACCENT}]{spin}[/]",
            f"[{conn_style}]{conn} gateway[/]",
            f"[{MAGENTA}]role: {self.role}[/]",
            f"telegram: {tg_char}",
            f"live: {self.activity}",
        ]
        self.update(Text.from_markup("  │  ".join(parts)))


# ── App ───────────────────────────────────────────────────────────────
class AntigonaConsole(App[None]):
    """Unified animated Antigona console (chat + activity + flows + approvals)."""

    TITLE = "Antigona Console"
    SUB_TITLE = "Live agent · orchestrator & executor · Telegram-linked"

    CSS = f"""
    Screen {{ background: {BG}; }}
    #panes {{ height: 1fr; }}
    #chat_pane {{ height: 1fr; }}
    #chat_log {{ height: 1fr; border: round {ACCENT}; }}
    #chat_input_row {{ height: 3; }}
    #chat_input {{ width: 1fr; }}
    #activity_table, #flows_table, #approvals_table {{ height: 1fr; }}
    #status_bar {{ height: 1; color: {MINT}; }}
    #role_bar {{ height: 1; color: {MAGENTA}; }}
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "show_tab('tab-chat')", "Chat"),
        Binding("2", "show_tab('tab-activity')", "Activity"),
        Binding("3", "show_tab('tab-flows')", "Flows"),
        Binding("4", "show_tab('tab-approvals')", "Approvals"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "approve_selected", "Approve"),
        Binding("x", "reject_selected", "Reject"),
        Binding("o", "toggle_role", "Role"),
    ]

    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:8090",
        token: str = "",
        refresh_interval: float = 1.0,
        session_id: str = "cli-session",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.gateway_url = gateway_url
        self.token = token
        self.refresh_interval = refresh_interval
        self.session_id = session_id
        self.client = CoreGatewayClient(base_url=gateway_url, token=token)
        self.selected_flow_id: str | None = None
        self.selected_approval_id: str | None = None
        self.role = "orchestrator"
        self._event_index = 0
        self._last_seq = 0
        self._activity_task: Any = None

    # ── Layout ──────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header()
        yield Tabs(*(Tab(title, id=tab_id) for tab_id, title, _ in TAB_SPECS), id="main_tabs")
        yield ContentSwitcher(
            self._chat_pane(),
            self._activity_pane(),
            self._flows_pane(),
            self._approvals_pane(),
            initial="pane-chat",
            id="panes",
        )
        yield StatusBar(id="status_bar")
        yield Footer()

    def _chat_pane(self) -> Vertical:
        return Vertical(
            Horizontal(Label("role: orchestrator", id="role_bar"), classes="role"),
            Log(id="chat_log", highlight=True),
            Horizontal(
                Input(placeholder="Message the agent… (/help for commands)", id="chat_input"),
                Button("Send", id="btn_send", variant="primary"),
                id="chat_input_row",
            ),
            id="pane-chat",
        )

    def _activity_pane(self) -> Vertical:
        return Vertical(
            Horizontal(
                Label("Live agent activity — everything the agent is doing"),
                Button("Refresh", id="btn_refresh_activity"),
                id="activity_bar",
            ),
            DataTable(id="activity_table"),
            id="pane-activity",
        )

    def _flows_pane(self) -> Vertical:
        return Vertical(
            Horizontal(
                Label("Flow ID: "),
                Input(placeholder="Enter flow ID", id="flow_id_input"),
                Button("Attach", id="btn_attach", variant="primary"),
                Button("Cancel", id="btn_cancel", variant="error"),
                Button("Refresh", id="btn_refresh_flows"),
                id="top_bar",
            ),
            DataTable(id="flows_table"),
            id="pane-flows",
        )

    def _approvals_pane(self) -> Vertical:
        return Vertical(
            DataTable(id="approvals_table"),
            Horizontal(
                Label("Approval ID: "),
                Input(placeholder="Enter approval ID", id="approval_id_input"),
                Button("Approve", id="btn_approve", variant="success"),
                Button("Reject", id="btn_reject", variant="warning"),
                Button("Refresh", id="btn_refresh_approvals"),
                id="approval_bar",
            ),
            id="pane-approvals",
        )

    # ── Lifecycle ───────────────────────────────────────────────────
    def on_mount(self) -> None:
        for table_id, cols in (
            ("#activity_table", EVENT_COLUMNS),
            ("#flows_table", FLOW_COLUMNS),
            ("#approvals_table", APPROVAL_COLUMNS),
        ):
            table = self.query_one(table_id, DataTable)
            table.add_columns(*cols)
            table.cursor_type = "row"
        self._chat_log("[*] Antigona Console started")
        self._chat_log(f"[*] gateway {self.gateway_url} · session {self.session_id}")
        self.check_connection()
        self.refresh_all()
        self.set_interval(self.refresh_interval, self.tick)
        self._activity_task = self.run_worker(
            self._event_loop(), name="events", group="events", exit_on_error=False
        )

    def on_unmount(self) -> None:
        task = self._activity_task
        if task is not None:
            task.cancel()

    def _chat_log(self, line: str) -> None:
        try:
            self.query_one("#chat_log", Log).write_line(line)
        except NoMatches:
            pass

    def _status(self) -> StatusBar:
        return self.query_one("#status_bar", StatusBar)

    def tick(self) -> None:
        self._status().animate_tick()

    # ── Connection / role ───────────────────────────────────────────
    def check_connection(self) -> None:
        async def _check() -> None:
            try:
                await self.client.get_events(after_seq=0, limit=1)
                self._status().connected = True
                self._chat_log("[*] gateway connected")
            except Exception as exc:
                self._status().connected = False
                self._chat_log(f"[!] gateway unreachable: {exc}")

        self.run_worker(_check(), group="conn", exit_on_error=False)

    def action_toggle_role(self) -> None:
        self.role = "executor" if self.role == "orchestrator" else "orchestrator"
        self._status().role = self.role
        try:
            self.query_one("#role_bar", Label).update(f"role: {self.role}")
        except NoMatches:
            pass
        self._chat_log(f"[*] role → {self.role} (free text still routes through the brain)")

    # ── Refresh ─────────────────────────────────────────────────────
    def action_refresh(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        self.run_worker(self.refresh_flows(), group="flows", exit_on_error=False)
        self.run_worker(self.refresh_approvals(), group="approvals", exit_on_error=False)
        self.run_worker(self.refresh_activity(), group="activity", exit_on_error=False)

    async def refresh_flows(self) -> None:
        try:
            view = await self.client.list_flows()
        except Exception as exc:
            self._chat_log(f"[!] flows refresh failed: {exc}")
            return
        raw = view if isinstance(view, list) else view.get("items", [])
        table = self.query_one("#flows_table", DataTable)
        table.clear()
        for f in raw:
            d = _flow_dict(f)
            table.add_row(
                str(d["id"]), _clip(d["goal"], 40),
                Text(str(d["status"]), style=status_color(str(d["status"]))),
                str(d["revision"]), _stamp(d["created_at"]),
                key=str(d["id"]),
            )
        self._chat_log(f"[*] flows: {len(raw)}")

    async def refresh_approvals(self) -> None:
        try:
            view = await self.client.list_approvals(status="PENDING")
        except Exception as exc:
            self._chat_log(f"[!] approvals refresh failed: {exc}")
            return
        raw = view.items if not isinstance(view, dict) else view.get("items", [])
        table = self.query_one("#approvals_table", DataTable)
        table.clear()
        for a in raw:
            if isinstance(a, dict):
                aid, task, tool, risk, reason = (
                    a.get("id"), a.get("task_id"), a.get("tool_name"),
                    a.get("risk_level"), a.get("reason"),
                )
            else:
                aid, task, tool, risk, reason = (
                    a.id, a.task_id, a.tool_name, a.risk_level, a.reason,
                )
            table.add_row(
                str(aid), str(task)[:8], str(tool),
                Text(str(risk), style=risk_color(str(risk))), _clip(reason, 40),
                key=str(aid),
            )
        self._chat_log(f"[*] pending approvals: {len(raw)}")

    async def refresh_activity(self) -> None:
        try:
            events = await self.client.get_events(after_seq=0, limit=50)
        except Exception as exc:
            self._chat_log(f"[!] activity refresh failed: {exc}")
            return
        table = self.query_one("#activity_table", DataTable)
        table.clear()
        for idx, ev in enumerate(events, start=1):
            table.add_row(*_event_row(idx, ev))
        table.scroll_end()
        self._status().activity = len(events)

    # ── Event loop (live monitor) ───────────────────────────────────
    async def _event_loop(self) -> None:
        """Tail the gateway events feed and append new events to the live table."""
        try:
            async for ev in self.client.connect_events(after_seq=self._last_seq):
                self._last_seq = int(ev.get("seq", self._last_seq))
                self._event_index += 1
                table = self.query_one("#activity_table", DataTable)
                table.add_row(*_event_row(self._event_index, ev))
                table.scroll_end()
                self._status().activity = self._event_index
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._chat_log(f"[!] event stream stopped: {exc}")

    # ── Chat (live agent conversation) ──────────────────────────────
    async def _send_chat(self, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        self._chat_log(f"[bold cyan]> {stripped}[/]")
        # Slash commands stay local when possible.
        if stripped.startswith("/"):
            await self._handle_slash(stripped)
            return
        turn_id = f"cli:turn:{uuid.uuid4().hex}"
        try:
            res = await self.client.send_dialogue_turn(
                text=stripped, session_id=self.session_id, channel="cli", user_id="default", turn_id=turn_id
            )
        except Exception as exc:
            self._chat_log(f"[bold red]gateway error: {exc}[/]")
            return
        response_type = str(res.get("response_type", "conversation"))
        reply = str(res.get("reply", "") or "")
        if response_type == "task_accepted":
            flow_id = str(res.get("flow_id", ""))
            self.selected_flow_id = flow_id
            self._chat_log(f"[yellow]task accepted · flow {flow_id[:12]}…[/]")
            # Live-wait the flow so the owner sees the agent work.
            try:
                await self.client.wait_for_terminal(flow_id, timeout=300, poll_interval=0.5)
                flow = await self.client.get_flow(flow_id)
                status = getattr(getattr(flow, "status", None), "value", None) or getattr(flow, "status", "?")
                self._chat_log(f"[green]✓ flow {flow_id[:12]}… → {status}[/]")
            except Exception as exc:
                self._chat_log(f"[bold red]flow wait failed: {exc}[/]")
        else:
            self._chat_log(f"[green]{reply or '…'}[/]")
        self.refresh_all()

    async def _handle_slash(self, cmd: str) -> None:
        verb, _, arg = cmd.partition(" ")
        arg = arg.strip()
        if verb in ("/help", "/?"):
            self._chat_log("[cyan]commands: /help /list /status <id> /approvals /approve <id> "
                      "/deny <id> /cancel <id> /memory /role[/]")
        elif verb in ("/list", "/flows"):
            await self.refresh_flows()
            self._chat_log("[*] flows listed above")
        elif verb == "/status":
            if arg:
                self.selected_flow_id = arg
            if self.selected_flow_id:
                try:
                    flow = await self.client.get_flow(self.selected_flow_id)
                    d = _flow_dict(flow)
                    self._chat_log(f"[yellow]flow {d['id']}: {d['status']} — {_clip(d['goal'], 60)}[/]")
                except Exception as exc:
                    self._chat_log(f"[bold red]{exc}[/]")
            else:
                self._chat_log("[!] /status <flow_id>")
        elif verb == "/approvals":
            await self.refresh_approvals()
        elif verb in ("/approve", "/deny"):
            if not arg:
                self._chat_log("[!] /approve <approval_id>")
                return
            approve = verb == "/approve"
            try:
                await self.client.decide_approval(arg, approve=approve)
                self._chat_log(f"[green]{'approved' if approve else 'denied'} {arg[:8]}…[/]")
                await self.refresh_approvals()
            except Exception as exc:
                self._chat_log(f"[bold red]{exc}[/]")
        elif verb == "/cancel":
            if arg:
                try:
                    await self.client.cancel(arg)
                    self._chat_log(f"[yellow]cancel requested for {arg[:12]}…[/]")
                except Exception as exc:
                    self._chat_log(f"[bold red]{exc}[/]")
            else:
                self._chat_log("[!] /cancel <flow_id>")
        elif verb == "/memory":
            try:
                data = await self.client.memory_list(limit=10)
                entries = data.get("items", data.get("entries", []))
                self._chat_log(f"[cyan]memory ({len(entries)} recent)[/]")
                for e in entries:
                    content = e.get("content") if isinstance(e, dict) else getattr(e, "content", "")
                    self._chat_log(f"  · {_clip(content, 70)}")
            except Exception as exc:
                self._chat_log(f"[bold red]{exc}[/]")
        elif verb == "/role":
            self.action_toggle_role()
        else:
            self._chat_log(f"[red]unknown command {verb} — try /help[/]")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat_input" and event.value:
            text = event.value
            event.input.value = ""
            self.run_worker(self._send_chat(text), group="chat", exit_on_error=False)

    # ── Approvals (buttons) ─────────────────────────────────────────
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        if event.data_table.id == "flows_table":
            self.selected_flow_id = str(key)
            self.query_one("#flow_id_input", Input).value = str(key)
        elif event.data_table.id == "approvals_table":
            self.selected_approval_id = str(key)
            self.query_one("#approval_id_input", Input).value = str(key)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if key is not None and event.data_table.id == "approvals_table":
            self.selected_approval_id = str(key)
            self.query_one("#approval_id_input", Input).value = str(key)

    def _current_approval_id(self) -> str:
        typed = self.query_one("#approval_id_input", Input).value.strip()
        return typed or (self.selected_approval_id or "")

    async def decide_selected(self, approve: bool) -> None:
        approval_id = self._current_approval_id()
        if not approval_id:
            self._chat_log("[!] no approval selected")
            return
        try:
            await self.client.decide_approval(approval_id, approve=approve)
            self._chat_log(f"[green]{'approved' if approve else 'denied'} {approval_id[:8]}…[/]")
        except Exception as exc:
            self._chat_log(f"[bold red]{exc}[/]")
        await self.refresh_approvals()

    def action_approve_selected(self) -> None:
        self.run_worker(self.decide_selected(True), group="decision", exit_on_error=False)

    def action_reject_selected(self) -> None:
        self.run_worker(self.decide_selected(False), group="decision", exit_on_error=False)

    # ── Buttons ─────────────────────────────────────────────────────
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_send":
            value = self.query_one("#chat_input", Input).value
            self.query_one("#chat_input", Input).value = ""
            if value:
                await self._send_chat(value)
        elif bid == "btn_refresh_activity":
            await self.refresh_activity()
        elif bid == "btn_refresh_flows":
            await self.refresh_flows()
        elif bid == "btn_refresh_approvals":
            await self.refresh_approvals()
        elif bid == "btn_attach":
            flow_id = self.query_one("#flow_id_input", Input).value.strip()
            if flow_id:
                self.selected_flow_id = flow_id
                self._chat_log(f"[*] attached flow {flow_id[:12]}…")
        elif bid == "btn_cancel":
            target = self.query_one("#flow_id_input", Input).value.strip() or self.selected_flow_id
            if target:
                try:
                    await self.client.cancel(target)
                    self._chat_log(f"[yellow]cancel requested for {target[:12]}…[/]")
                    await self.refresh_flows()
                except Exception as exc:
                    self._chat_log(f"[bold red]{exc}[/]")
        elif bid == "btn_approve":
            await self.decide_selected(True)
        elif bid == "btn_reject":
            await self.decide_selected(False)

    # ── Tabs ────────────────────────────────────────────────────────
    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or ""
        if event.tabs.id == "main_tabs":
            pane_id = MAIN_PANES.get(tab_id)
            if pane_id:
                try:
                    self.query_one("#panes", ContentSwitcher).current = pane_id
                except NoMatches:
                    pass

    def action_show_tab(self, tab_id: str) -> None:
        try:
            self.query_one("#main_tabs", Tabs).active = tab_id
        except NoMatches:
            pass


def main() -> None:
    app = AntigonaConsole()
    app.run()


if __name__ == "__main__":
    main()
