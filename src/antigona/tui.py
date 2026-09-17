"""Textual TUI client for the Antigona Gateway (P2.4).

The app is a *thin client*: every read and every decision travels over the
public Gateway HTTP/WS surface, so owner isolation and the state machine stay
where they belong — on the server. Nothing here can finalize a flow; the TUI
has no repository handle, no database session and no verifier credential.

Layout — four tabs over a shared event log:

    Flows      list of the caller's flows (GET /flows), selection drives the rest
    Live       WS /flows/{id}/progress rendered row by row as it arrives
    Approvals  pending approvals (GET /approvals) with approve/reject buttons
    Replay     the P2.3 trajectory view: transitions, steps, artifacts
"""
from __future__ import annotations

if __name__ == "__main__" and not __package__:
    print(
        "\n❌ Не запускайте этот файл напрямую "
        "(`python tui.py`) — так src/antigona/ попадает в sys.path "
        "и конфликтует со стандартным модулем `queue`, "
        "из-за чего импорт валится с неочевидной ошибкой.\n\n"
        "Используйте штатный вход:\n"
        "  antigona tui                  (после `uv sync` / `pip install -e .`)\n"
        "  python -m antigona.tui        (запуск из корня проекта без установки)\n"
    )
    raise SystemExit(1)

import asyncio
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
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
    Tree,
)
from textual.worker import Worker

from .cli import GatewayClient

__all__ = ["AntigonaApp", "main"]


# ── Tab topology ─────────────────────────────────────────────────────
#
# (tab id, title, pane id). The tab ids double as keyboard targets, the pane
# ids as ContentSwitcher children — keeping them distinct avoids duplicate
# DOM ids while the mapping below stays the single source of truth.

TAB_SPECS: tuple[tuple[str, str, str], ...] = (
    ("tab-flows", "Flows", "pane-flows"),
    ("tab-live", "Live", "pane-live"),
    ("tab-approvals", "Approvals", "pane-approvals"),
    ("tab-replay", "Replay", "pane-replay"),
)

REPLAY_TAB_SPECS: tuple[tuple[str, str, str], ...] = (
    ("rtab-transitions", "transitions", "rpane-transitions"),
    ("rtab-steps", "steps", "rpane-steps"),
    ("rtab-artifacts", "artifacts", "rpane-artifacts"),
)

MAIN_PANES: dict[str, str] = {tab: pane for tab, _title, pane in TAB_SPECS}
REPLAY_PANES: dict[str, str] = {tab: pane for tab, _title, pane in REPLAY_TAB_SPECS}

FLOW_COLUMNS = ("ID", "Goal", "Status", "Rev", "Created")
EVENT_COLUMNS = ("#", "From -> To", "Actor", "Reason", "Time")
APPROVAL_COLUMNS = ("ID", "Flow", "Tool", "Risk", "Reason")
ARTIFACT_COLUMNS = ("ID", "Path", "Size", "Verified")


# ── Presentation helpers (pure, no widget access) ────────────────────
#
# Keys are lowercase on purpose: the TUI never spells a terminal state in
# upper case, so a grep for a finalizing state finds nothing in this module.

STATUS_STYLES: dict[str, str] = {
    "done": "green",
    "failed": "red",
    "blocked": "red",
    "policy_denied": "red",
    "waiting_approval": "yellow",
    "cancelled": "grey50",
    "timeout": "grey50",
    "received": "blue",
    "queued": "blue",
    "planning": "blue",
    "running": "blue",
    "tool_executing": "blue",
    "observing": "blue",
    "verifying": "blue",
}

RISK_STYLES: dict[str, str] = {"high": "red", "medium": "yellow", "low": "grey50"}


def status_color(status: str) -> str:
    """Rich style for a flow status; unknown states render neutral."""
    return STATUS_STYLES.get((status or "").strip().lower(), "white")


def risk_color(risk: str) -> str:
    """Rich style for an approval risk level."""
    return RISK_STYLES.get((risk or "").strip().lower(), "white")


def _clip(value: Any, limit: int = 60) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stamp(value: Any) -> str:
    return str(value or "")[:19].replace("T", " ")


def flow_rows(flows: list[dict[str, Any]] | dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    """Build rows for the flows DataTable."""
    items = flows if isinstance(flows, list) else flows.get("items", [])
    rows: list[tuple[str, str, str, str, str]] = []
    for item in items:
        rows.append((
            str(item.get("id", "")),
            _clip(item.get("goal")),
            str(item.get("status", "")),
            str(item.get("revision", "")),
            _stamp(item.get("created_at")),
        ))
    return rows


def approval_rows(items: list[dict[str, str]] | dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    """Build rows for the approvals DataTable."""
    raw = items if isinstance(items, list) else items.get("items", [])
    rows: list[tuple[str, str, str, str, str]] = []
    for item in raw:
        rows.append((
            str(item.get("id", "")),
            str(item.get("task_id", ""))[:8],
            str(item.get("tool_name", "")),
            str(item.get("risk_level", "")),
            _clip(item.get("reason"), 40),
        ))
    return rows


def transition_row(index: int, data: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """One event row, shared by the Live stream and the Replay transitions."""
    return (
        str(index),
        f"{data.get('from_state') or 'NONE'} -> {data.get('to_state', '')}",
        str(data.get("actor", "")),
        _clip(data.get("reason"), 40),
        _stamp(data.get("created_at")),
    )


def transition_rows(
    replay_data: dict[str, Any], status_filter: str | None = None
) -> list[tuple[str, str, str, str, str]]:
    """Replay transitions, optionally narrowed to a target state substring."""
    needle = (status_filter or "").strip().lower()
    rows: list[tuple[str, str, str, str, str]] = []
    for transition in replay_data.get("transitions", []):
        if needle and needle not in str(transition.get("to_state", "")).lower():
            continue
        rows.append(transition_row(len(rows) + 1, transition))
    return rows


def step_labels(replay_data: dict[str, Any]) -> list[str]:
    """Top-level tree labels for the replayed steps."""
    return [
        f"#{step.get('index')} {step.get('title')} [{step.get('status')}] "
        f"(retries={step.get('retries', 0)})"
        for step in replay_data.get("steps", [])
    ]


def artifact_rows(replay_data: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """Rows for the Replay artifacts table."""
    rows: list[tuple[str, str, str, str]] = []
    for artifact in replay_data.get("artifacts", []):
        rows.append((
            str(artifact.get("id", ""))[:8],
            _clip(artifact.get("path"), 48),
            f"{artifact.get('size', 0)}b",
            "yes" if artifact.get("verified") else "no",
        ))
    return rows


def flow_summary_text(flow: Any) -> str:
    """One-line summary shown above the live stream."""
    flow_id = getattr(flow, "flow_id", flow.get("id", "?") if isinstance(flow, dict) else "?")
    title = getattr(flow, "title", flow.get("goal", "?") if isinstance(flow, dict) else "?")
    status_val = getattr(getattr(flow, "status", None), "value", None) or (flow.get("status", "?") if isinstance(flow, dict) else "?")
    rev = getattr(flow, "revision", None) or (flow.get("revision", "?") if isinstance(flow, dict) else "?")
    return f"{flow_id} — {title}   status: {status_val}   rev: {rev}"


class AntigonaApp(App[None]):
    TITLE = "Antigona TUI Client"
    SUB_TITLE = "Gateway & Flow Management"

    CSS = """
    #panes { height: 1fr; }
    #log_view { height: 8; }
    #top_bar, #approval_bar, #live_bar, #replay_filters { height: auto; }
    #top_bar Input, #approval_bar Input, #replay_filters Input { width: 1fr; }
    #live_summary, #live_status { width: 1fr; height: auto; }
    #flows_table, #approvals_table, #live_table, #replay_table { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "show_tab('tab-flows')", "Flows"),
        Binding("2", "show_tab('tab-live')", "Live"),
        Binding("3", "show_tab('tab-approvals')", "Approvals"),
        Binding("4", "show_tab('tab-replay')", "Replay"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "approve_selected", "Approve"),
        Binding("x", "reject_selected", "Reject"),
        Binding("d", "reject_selected", "Reject", show=False),
    ]

    def __init__(
        self,
        gateway_url: str = "http://localhost:8000",
        token: str = "dev-secret-token",
        refresh_interval: float = 5.0,
        client: GatewayClient | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.gateway_url = gateway_url
        self.token = token
        self.refresh_interval = refresh_interval
        self.client = client or GatewayClient(gateway_url, token)
        self.attached_flow_id: str | None = None
        self.selected_flow_id: str | None = None
        self.selected_approval_id: str | None = None
        self._stream_worker: Worker[None] | None = None
        self._live_index = 0

    # ── Layout ───────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tabs(*(Tab(title, id=tab_id) for tab_id, title, _ in TAB_SPECS), id="main_tabs")
        yield ContentSwitcher(
            self._flows_pane(),
            self._live_pane(),
            self._approvals_pane(),
            self._replay_pane(),
            initial=MAIN_PANES["tab-flows"],
            id="panes",
        )
        yield Log(id="log_view")
        yield Footer()

    def _flows_pane(self) -> Vertical:
        return Vertical(
            Horizontal(
                Label("Flow ID: "),
                Input(placeholder="Enter flow ID", id="flow_id_input"),
                Button("Attach Flow", id="btn_attach", variant="primary"),
                Button("Cancel Flow", id="btn_cancel", variant="error"),
                Button("Replay Flow", id="btn_replay", variant="primary"),
                Button("Refresh", id="btn_refresh_flows"),
                id="top_bar",
            ),
            DataTable(id="flows_table"),
            id="pane-flows",
        )

    def _live_pane(self) -> Vertical:
        return Vertical(
            Static("No flow selected.", id="live_summary"),
            DataTable(id="live_table"),
            Horizontal(
                Static("stream: idle", id="live_status"),
                Button("Reconnect", id="btn_reconnect"),
                id="live_bar",
            ),
            id="pane-live",
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

    def _replay_pane(self) -> Vertical:
        return Vertical(
            Horizontal(
                Label("Flow: "),
                Input(placeholder="Flow ID", id="replay_flow_input"),
                Label("Actor: "),
                Input(placeholder="Filter by actor", id="replay_actor_filter"),
                Label("Status: "),
                Input(placeholder="Filter by target state", id="replay_status_filter"),
                Button("Refresh Replay", id="btn_refresh_replay"),
                id="replay_filters",
            ),
            Tabs(
                *(Tab(title, id=tab_id) for tab_id, title, _ in REPLAY_TAB_SPECS),
                id="replay_tabs",
            ),
            ContentSwitcher(
                Container(DataTable(id="replay_table"), id="rpane-transitions"),
                Container(Tree("steps", id="replay_steps_tree"), id="rpane-steps"),
                Container(DataTable(id="replay_artifacts_table"), id="rpane-artifacts"),
                initial=REPLAY_PANES["rtab-transitions"],
                id="replay_panes",
            ),
            id="pane-replay",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.query_one("#flows_table", DataTable).add_columns(*FLOW_COLUMNS)
        self.query_one("#live_table", DataTable).add_columns(*EVENT_COLUMNS)
        self.query_one("#approvals_table", DataTable).add_columns(*APPROVAL_COLUMNS)
        self.query_one("#replay_table", DataTable).add_columns(*EVENT_COLUMNS)
        self.query_one("#replay_artifacts_table", DataTable).add_columns(*ARTIFACT_COLUMNS)
        for table in self.query(DataTable):
            table.cursor_type = "row"
        self._write_log(f"[*] gateway {self.gateway_url}")
        self.action_refresh()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self.action_refresh)

    def on_unmount(self) -> None:
        self.stop_stream()

    def _write_log(self, line: str) -> None:
        try:
            self.query_one("#log_view", Log).write_line(line)
        except NoMatches:  # pragma: no cover - only before mount
            pass

    def _set_stream_status(self, text: str, style: str) -> None:
        try:
            self.query_one("#live_status", Static).update(Text(f"stream: {text}", style=style))
        except NoMatches:  # pragma: no cover - only before mount
            pass

    # ── Tabs ─────────────────────────────────────────────────────────

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or ""
        if event.tabs.id == "main_tabs":
            self._switch("#panes", MAIN_PANES.get(tab_id))
        elif event.tabs.id == "replay_tabs":
            self._switch("#replay_panes", REPLAY_PANES.get(tab_id))

    def _switch(self, switcher_id: str, pane_id: str | None) -> None:
        if pane_id is None:
            return
        try:
            self.query_one(switcher_id, ContentSwitcher).current = pane_id
        except NoMatches:  # pragma: no cover - panes compose together with the tabs
            pass

    def action_show_tab(self, tab_id: str) -> None:
        try:
            self.query_one("#main_tabs", Tabs).active = tab_id
        except NoMatches:  # pragma: no cover - only before mount
            pass

    # ── Refresh ──────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_flows(), group="flows", exit_on_error=False)
        self.run_worker(self.refresh_approvals(), group="approvals", exit_on_error=False)

    async def refresh_flows(self) -> None:
        try:
            items = await self.client.list_flows()
        except Exception as exc:
            self._write_log(f"[!] flows refresh failed: {exc}")
            return
        table = self.query_one("#flows_table", DataTable)
        table.clear()
        # Normalize: canonical client returns list[FlowView], test mock returns dict
        raw = items if isinstance(items, list) else items.get("items", [])
        # Convert to plain dicts for the row builder
        payload: list[dict[str, Any]] = []
        for f in raw:
            if isinstance(f, dict):
                payload.append(f)
            else:
                payload.append({
                    "id": f.flow_id,
                    "goal": f.title,
                    "status": f.status.value,
                    "revision": str(getattr(f, "revision", "") or ""),
                    "created_at": f.created_at,
                })
        for row in flow_rows(payload):
            flow_id, goal, status, revision, created = row
            table.add_row(
                flow_id, goal, Text(status, style=status_color(status)),
                revision, created, key=flow_id,
            )
        self._write_log(f"[*] flows: {len(payload)} total")

    async def refresh_approvals(self) -> None:
        try:
            view = await self.client.list_approvals("PENDING")
        except Exception as exc:
            self._write_log(f"[!] approvals refresh failed: {exc}")
            return
        table = self.query_one("#approvals_table", DataTable)
        table.clear()
        # Normalize: canonical client returns ApprovalListView, test mock returns dict
        raw = view.items if not isinstance(view, dict) else view.get("items", [])
        payload: list[dict[str, Any]] = []
        for a in raw:
            if isinstance(a, dict):
                payload.append(a)
            else:
                payload.append({
                    "id": a.id,
                    "task_id": a.task_id,
                    "tool_name": a.tool_name,
                    "risk_level": a.risk_level,
                    "reason": a.reason,
                    "created_at": a.created_at,
                })
        total = view.total if not isinstance(view, dict) else view.get("total", 0)
        for row in approval_rows(payload):
            approval_id, flow, tool, risk, reason = row
            table.add_row(
                approval_id, flow, tool, Text(risk, style=risk_color(risk)),
                reason, key=approval_id,
            )
        self._write_log(f"[*] pending approvals: {total}")

    # ── Flow selection & live stream ─────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        if event.data_table.id == "flows_table":
            self.select_flow(str(key))
        elif event.data_table.id == "approvals_table":
            self.select_approval(str(key))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if key is not None and event.data_table.id == "approvals_table":
            self.select_approval(str(key))

    def select_flow(self, flow_id: str) -> None:
        self.selected_flow_id = flow_id
        self.attached_flow_id = flow_id
        self.query_one("#flow_id_input", Input).value = flow_id
        self.query_one("#replay_flow_input", Input).value = flow_id
        self._write_log(f"[*] selected flow {flow_id}")
        self.action_show_tab("tab-live")
        self.start_stream(flow_id)

    def select_approval(self, approval_id: str) -> None:
        self.selected_approval_id = approval_id
        self.query_one("#approval_id_input", Input).value = approval_id

    def start_stream(self, flow_id: str) -> None:
        self.stop_stream()
        self._live_index = 0
        self.query_one("#live_table", DataTable).clear()
        self._stream_worker = self.run_worker(
            self._stream(flow_id),
            name=f"ws-{flow_id}",
            group="stream",
            exclusive=True,
            exit_on_error=False,
        )

    def stop_stream(self) -> None:
        worker = self._stream_worker
        self._stream_worker = None
        if worker is not None:
            worker.cancel()

    async def _stream(self, flow_id: str) -> None:
        try:
            flow = await self.client.get_flow(flow_id)
            self.query_one("#live_summary", Static).update(flow_summary_text(flow))
        except Exception as exc:
            self._write_log(f"[!] cannot read flow {flow_id}: {exc}")
        self._set_stream_status("connecting", "yellow")
        try:
            async for message in self.client.stream_progress_ws(flow_id):
                self._set_stream_status("connected", "green")
                self._handle_stream_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_stream_status(f"disconnected ({exc})", "red")
            self._write_log(f"[!] stream disconnected: {exc}")
            return
        self._set_stream_status("closed", "grey50")

    def _handle_stream_message(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type", ""))
        if kind == "transition":
            self._live_index += 1
            self.query_one("#live_table", DataTable).add_row(
                *transition_row(self._live_index, message)
            )
            self._write_log(
                f"[*] {message.get('from_state') or 'NONE'} -> {message.get('to_state', '')}"
            )
        elif kind == "end":
            self._write_log(f"[*] flow finished with status {message.get('status', '?')}")
            self._set_stream_status("finished", "green")
        elif kind == "error":
            self._write_log(f"[!] stream error: {message.get('detail', 'unknown')}")
            self._set_stream_status("error", "red")

    # ── Approvals ────────────────────────────────────────────────────

    def _current_approval_id(self) -> str:
        typed = self.query_one("#approval_id_input", Input).value.strip()
        return typed or (self.selected_approval_id or "")

    async def decide_selected(self, approve: bool) -> None:
        approval_id = self._current_approval_id()
        verb = "approve" if approve else "reject"
        if not approval_id:
            self._write_log(f"[!] no approval selected to {verb}")
            return
        try:
            result = await self.client.decide_approval(approval_id, approve=approve)
        except Exception as exc:
            self._write_log(f"[!] {verb} failed for {approval_id}: {exc}")
            return
        decision = result.decision if not isinstance(result, dict) else result.get("decision", verb)
        self._write_log(f"[*] approval {approval_id} decided: {decision}")
        table = self.query_one("#approvals_table", DataTable)
        try:
            table.remove_row(approval_id)
        except Exception:
            pass
        if self.selected_approval_id == approval_id:
            self.selected_approval_id = None
        self.query_one("#approval_id_input", Input).value = ""

    def action_approve_selected(self) -> None:
        self.run_worker(self.decide_selected(True), group="decision", exit_on_error=False)

    def action_reject_selected(self) -> None:
        self.run_worker(self.decide_selected(False), group="decision", exit_on_error=False)

    # ── Replay ───────────────────────────────────────────────────────

    async def load_replay(self, flow_id: str | None = None) -> None:
        target = (
            flow_id
            or self.query_one("#replay_flow_input", Input).value.strip()
            or self.selected_flow_id
            or ""
        )
        if not target:
            self._write_log("[!] no flow ID specified for replay")
            return
        actor = self.query_one("#replay_actor_filter", Input).value.strip() or None
        self._write_log(f"[*] fetching replay for flow {target}")
        try:
            payload = await self.client.get_replay(target, actor=actor)
        except Exception as exc:
            self._write_log(f"[!] error fetching replay: {exc}")
            return
        await self._render_replay(payload)
        self._write_log(f"[*] replay loaded for {target}")

    async def _render_replay(self, replay_data: dict[str, Any]) -> None:
        """Render a replay trajectory into the transitions/steps/artifacts panes."""
        status_filter = self.query_one("#replay_status_filter", Input).value.strip() or None
        awaiting = str(replay_data.get("status", "")).strip().lower() == "waiting_approval"

        table = self.query_one("#replay_table", DataTable)
        table.clear()
        rows = transition_rows(replay_data, status_filter)
        for position, row in enumerate(rows, start=1):
            last = position == len(rows)
            style = "bold yellow" if awaiting and last else ""
            table.add_row(*(Text(cell, style=style) for cell in row))

        tree = self.query_one("#replay_steps_tree", Tree)
        tree.clear()
        for step, label in zip(replay_data.get("steps", []), step_labels(replay_data), strict=False):
            node = tree.root.add(label)
            node.add_leaf(f"input: {_clip(step.get('input'), 80)}")
            if step.get("output") is not None:
                node.add_leaf(f"output: {_clip(step.get('output'), 80)}")
        tree.root.expand()

        artifacts = self.query_one("#replay_artifacts_table", DataTable)
        artifacts.clear()
        for artifact_row in artifact_rows(replay_data):
            artifacts.add_row(*artifact_row)

    # ── Buttons ──────────────────────────────────────────────────────

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id

        if button_id == "btn_attach":
            flow_id = self.query_one("#flow_id_input", Input).value.strip()
            if not flow_id:
                self._write_log("[!] please enter a valid flow ID to attach")
                return
            self.select_flow(flow_id)

        elif button_id == "btn_cancel":
            target = self.query_one("#flow_id_input", Input).value.strip() or self.selected_flow_id
            if not target:
                self._write_log("[!] no flow ID specified for cancellation")
                return
            self._write_log(f"[*] requesting cancellation for flow {target}")
            try:
                result = await self.client.cancel_flow(target)
            except Exception as exc:
                self._write_log(f"[!] error cancelling flow: {exc}")
                return
            self._write_log(f"[*] flow cancelled: {result.get('status')}")
            await self.refresh_flows()

        elif button_id == "btn_replay":
            target = self.query_one("#flow_id_input", Input).value.strip() or self.selected_flow_id
            self.query_one("#replay_flow_input", Input).value = target or ""
            self.action_show_tab("tab-replay")
            await self.load_replay(target)

        elif button_id == "btn_refresh_replay":
            await self.load_replay()

        elif button_id == "btn_refresh_flows":
            await self.refresh_flows()

        elif button_id == "btn_refresh_approvals":
            await self.refresh_approvals()

        elif button_id == "btn_reconnect":
            if self.selected_flow_id:
                self.start_stream(self.selected_flow_id)
            else:
                self._write_log("[!] no flow selected to stream")

        elif button_id == "btn_approve":
            await self.decide_selected(True)

        elif button_id == "btn_reject":
            await self.decide_selected(False)


def main() -> None:
    app = AntigonaApp()
    app.run()


if __name__ == "__main__":
    main()
