"""Live-chat ChatController over Gateway presentation boundary.

Pure thin-client coordinator managing prompt input, safe Checkpoint 4 command
dispatch, and bounded polling over an injected GatewayClient. Does NOT own
durable sessions, memory, providers, tools, or DB runtime.

Free text is handled as a *turn*: natural approval first, then a single call
to the canonical Gateway Turn API (``/api/v1/dialogue/turn``). Stage 1: the
CLI is a THIN client — it has NO local IntentRouter/DialogueEngine; the server
core classifies and routes, and the returned ``response_type`` decides
rendering (conversation/clarification → reply; task_accepted → live wait on
the returned flow; control/error → reply). If the Gateway is unavailable the
controller fails CLOSED with an honest message and never starts a local
engine. While a decision is pending the user can approve or deny in natural
language ("да" / "нет") without IDs. Typed slash commands (``/list``,
``/status``, ``/approve``, ``/deny``, …) remain the explicit fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
import uuid
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prompt_toolkit.formatted_text import HTML

if TYPE_CHECKING:
    from prompt_toolkit.shortcuts import PromptSession
    from rich.spinner import Spinner

from antigona.cli_ui.approval_picker import PickerAction, pick_approval
from antigona.cli_ui.command_menu import build_command_toolbar, merge_catalog
from antigona.cli_ui.commands import (
    CommandDisposition,
    CommandKind,
    GatewayClientProtocol,
    dispatch_command,
    parse_command,
)
from antigona.cli_ui.flow_adapter import adapt_flow_status
from antigona.cli_ui.models import (
    ChatMessage,
    ChatMessageRole,
    ChatUIState,
    TerminalOutcome,
    TerminalOutcomeStatus,
)
from antigona.cli_ui.prompts import create_prompt_session, read_prompt
from antigona.cli_ui.renderer import CliRenderer
from antigona.cli_ui.status import (
    render_approval_list,
    render_commands_list,
    render_flow_list,
    render_flow_status,
    render_health,
    render_memory_list,
    render_outcome,
    render_session_history,
    render_session_info,
)
from antigona.cli_ui.status_bar import build_pipeline_bar
from antigona.conversation.dialogue_engine import DialogueEngine
from antigona.core.paths import owner_dir
from antigona.security.auth_service import cli_principal

logger = logging.getLogger(__name__)

#: Spinner text per panel status (calm, human-readable phase labels).
_PHASE_TEXT: dict[str, str] = {
    "sending": "отправка запроса…",
    "planning": "🧠 планирование…",
    "tool_executing": "🛠 выполнение инструмента…",
    "observing": "👁 наблюдение результата…",
    "verifying": "🛡 проверка Verifier…",
    "waiting_approval": "🔐 требуется подтверждение…",
    "done": "✅ готово",
    "failed": "❌ ошибка",
    "cancelled": "⛔ отменено",
    "reconnecting": "🔄 переподключение…",
    "timeout": "❌ таймаут",
    "error": "❌ ошибка",
}



def _system_info_text() -> str:
    """Collect read-only host system info from /proc (no subprocess/network).

    Returns a compact, human-readable block. Every value is read live from
    kernel pseudo-files, so it never blocks on external services.
    """
    lines: list[str] = ["🖥 Система (read-only)"]
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            secs = float(f.read().split()[0])
        uptime = f"{int(secs // 86400)}д {int((secs % 86400) // 3600)}ч {int((secs % 3600) // 60)}м"
        lines.append(f"  ⏱ Аптайм: {uptime}")
    except (OSError, ValueError, IndexError):
        lines.append("  ⏱ Аптайм: недоступен")

    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            load = f.read().split()
        lines.append(f"  📈 Load: {load[0]} {load[1]} {load[2]} (1/5/15 мин)")
    except (OSError, IndexError):
        pass

    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem = {}
            for ln in f:
                parts = ln.split()
                if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0].rstrip(":")] = int(parts[1]) // 1024
        if mem:
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", 0)
            used = total - avail
            pct = int(used / total * 100) if total else 0
            lines.append(f"  🧠 RAM: {used}MB / {total}MB ({pct}%)")
    except (OSError, ValueError):
        pass

    lines.append(f"  🧵 CPU: {os.cpu_count() or '?'} ядер")
    # BUG ANT-007 (wave3, class G2): os.uname() does not exist on Windows.
    _uname_fn = getattr(os, "uname", None)
    if _uname_fn is not None:
        try:
            _uname = _uname_fn()
            lines.append(
                f"  🖥 Хост: {_uname.nodename} ({_uname.sysname} {_uname.release})"
            )
        except OSError:
            import platform
            lines.append(f"  🖥 Хост: {platform.node()} ({platform.system()} {platform.release()})")
    else:
        import platform
        lines.append(f"  🖥 Хост: {platform.node()} ({platform.system()} {platform.release()})")

    try:
        du = shutil.disk_usage(str(owner_dir()))
        gb = 1024 ** 3
        lines.append(
            f"  💾 Диск: {du.used / gb:.1f}G / {du.total / gb:.1f}G ({du.free / gb:.1f}G свободно)"
        )
    except (OSError, ValueError):
        pass

    return "\n".join(lines)


def _export_transcript(state: Any, path_arg: str) -> str:
    """Write ``state.messages`` to a markdown file and return the resolved path.

    *path_arg* empty -> ``~/antigona_chat_<session>_<ts>.md``.
    *path_arg* given -> expanded, but bounded to the owner's home (rejects
    absolute paths and ``..`` traversal) so an exported file can never escape
    ``~``. Raises ``ValueError`` on invalid paths.
    """
    import datetime

    messages = getattr(state, "messages", []) or []
    session_id = str(getattr(state, "session_id", "session") or "session")
    safe_session = "".join(c for c in session_id if c.isalnum() or c in "-_") or "session"

    base = owner_dir()
    if path_arg:
        raw = path_arg
        if raw.startswith("~/"):
            raw = raw[2:]
        elif raw.startswith("./"):
            raw = raw[2:]
        if os.path.isabs(raw) or raw.startswith("~"):
            raise ValueError("Путь должен быть относительным (в пределах домашней папки).")
        # BUG ANT-007 (wave3, class G1c): split(os.sep) misses "/" separators
        # on Windows (os.sep == "\\"), letting "../evil.md" traversal slip
        # through. Split on both separators.
        import re as _re
        parts = [p for p in _re.split(r"[\\/]", raw) if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError("Путь не должен содержать '..'.")
        out_path = os.path.join(str(base), *parts)
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(str(base), f"antigona_chat_{safe_session}_{ts}.md")

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        raise ValueError(f"Каталог не существует: {out_dir}")

    role_labels = {
        "user": "Пользователь",
        "assistant": "Антигона",
        "system": "Система",
        "tool": "Инструмент",
        "error": "Ошибка",
        "warning": "Предупреждение",
        "info": "Инфо",
    }
    lines = ["# Экспорт диалога Antigona", f"- Сессия: {session_id}", f"- Сообщений: {len(messages)}", ""]
    for m in messages:
        role = str(getattr(m, "role", "info"))
        content = str(getattr(m, "content", ""))
        label = role_labels.get(role, role)
        lines.append(f"**{label}:**")
        lines.append("")
        lines.append(content)
        lines.append("")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path

def _phase_text(status: str) -> str:
    """Return the spinner text for a panel status (ASCII-safe fallback)."""
    return _PHASE_TEXT.get(status, f"… {status}")


class RendererProtocol(Protocol):
    """Strict renderer protocol for CLI presentation."""

    def render_message(
        self,
        role: ChatMessageRole | str,
        content: str,
        width: int | None = None,
    ) -> None: ...

    def render_state(self, state: ChatUIState) -> None: ...

    def render_outcome(self, outcome: TerminalOutcome) -> None: ...

    def render_panel(self, state: ChatUIState) -> None: ...

    def update_panel(self, state: ChatUIState) -> None: ...

    def clear_terminal(self) -> None: ...

    def clear_and_repaint(self, state: ChatUIState) -> None: ...

    def live_spinner(
        self,
        status_text: str = "Processing...",
        spinner_name: str = "dots",
    ) -> AbstractContextManager[Spinner | None]: ...

    def release(self) -> None: ...


@runtime_checkable
class TerminalWaiterProtocol(Protocol):
    """Canonical terminal-wait capability of ``antigona.core.gateway_client.GatewayClient``.

    Kept separate from :class:`GatewayClientProtocol` on purpose: waiting for a
    terminal state is an optional capability of an injected client, and it is the
    only path allowed to open the presentation success gate.
    """

    async def wait_for_terminal(
        self,
        flow_id: str,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.25,
        cancel_event: asyncio.Event | None = None,
    ) -> Any: ...


def _usage_for(command_name: str) -> str:
    """Return usage hint for a known command that received malformed arguments."""
    usage_map: dict[str, str] = {
        "/get": "Usage: /get <flow_id>\n\nGet the result of a completed flow.",
        "/status": "Usage: /status <flow_id>\n\nGet the status of a specific flow.",
        "/approve": "Usage: /approve <approval_id>\n\nApprove a pending decision.",
        "/deny": "Usage: /deny <approval_id>\n\nDeny a pending decision.",
        "/cancel": "Usage: /cancel <flow_id>\n\nCancel a running flow.",
        "/steer": "Usage: /steer <flow_id> <text>\n\nCorrect a running flow.",
        "/list": "Usage: /list\n\nList all active flows.",
        "/tasks": "Usage: /tasks\n\nList all active flows (alias of /list).",
        "/health": "Usage: /health\n\nCheck Gateway availability.",
        "/commands": "Usage: /commands\n\nList system commands from the registry.",
        "/session": "Usage: /session <session_id>\n\nShow session info.",
        "/history": "Usage: /history <session_id>\n\nShow session history.",
        "/memory": "Usage: /memory [text]\n\nShow agent memory (or store text).",
        "/approvals": "Usage: /approvals\n\nList pending approvals.",
        "/help": "Usage: /help\n\nShow this help message.",
        "/exit": "Usage: /exit or /quit\n\nExit the CLI.",
        "/shell": "Usage: /shell <command> (alias /sh)\n\nRun a host command as the elevated owner. Requires the PIN owner-mode gate at CLI startup.",
    }
    return usage_map.get(
        command_name, f"Unexpected arguments for {command_name}. Type /help for available commands."
    )


def _slash_suggestions(command_name: str) -> str:
    """Return 'Unknown command' message with suggestions for a slash-prefixed input."""
    if command_name == "/steer":
        return (
            f"Command not implemented: {command_name}.\n\n"
            "This command is recognised but not yet implemented. "
            "Available commands:\n"
            "  /help     — show help\n"
            "  /get      — get flow result\n"
            "  /status   — get flow status\n"
            "  /list     — list flows\n"
            "  /tasks    — list flows (alias)\n"
            "  /approvals — list pending approvals\n"
            "  /approve  — approve a decision\n"
            "  /deny     — deny a decision\n"
            "  /cancel   — cancel a flow"
        )
    return (
        f"Unknown command: {command_name}.\n\n"
        "Did you mean:\n"
        "  /help       — show help\n"
        "  /get        — get flow result\n"
        "  /status     — get flow status\n"
        "  /list       — list flows\n"
        "  /tasks      — list flows (alias)\n"
        "  /approvals  — list pending approvals\n"
        "  /approve    — approve a decision\n"
        "  /deny       — deny a decision\n"
        "  /cancel     — cancel a flow\n"
        "  /steer      — correct a running flow\n"
        "  /health     — check Gateway availability\n"
        "  /commands   — list system commands\n"
        "  /session    — session info\n"
        "  /history    — session history\n"
        "  /memory     — agent memory"
    )


# ── Natural-language turn vocabulary (CLI conversation layer) ─────────────────
# Only consulted while the CLI is actually waiting at a pending approval, so
# these words never hijack task phrasing ("давай создай файл…" still routes as
# a task when no decision is pending).

_APPROVE_WORDS = frozenset(
    {
        "да",
        "разрешаю",
        "разреши",
        "подтверждаю",
        "подтверди",
        "ок",
        "ok",
        "yes",
        "y",
        "согласен",
        "согласна",
        "давай",
        "продолжай",
        "продолжить",
        "го",
        "continue",
    }
)

_DENY_WORDS = frozenset(
    {
        "нет",
        "отклони",
        "отклонить",
        "не разрешаю",
        "не надо",
        "отмена",
        "no",
        "n",
        "стоп",
        "хватит",
        "не согласен",
        "отказать",
    }
)

#: How long to keep polling when the flow is WAITING_APPROVAL but the approval
#: row has not materialised yet (worker mid-transition), before showing the
#: neutral "awaiting decision" message and returning to the prompt.
_APPROVAL_GRACE_SECONDS = 5.0


def _approval_word_decision(text: str) -> bool | None:
    """Map a user turn to an approval decision while one is pending.

    Returns True (approve), False (deny), or None when the text is not an
    approval response. Denial has priority: a standalone deny word anywhere in
    the phrase ("да нет, погоди" → deny) beats a first-token approval match.
    The deny set deliberately excludes the bare word "не", so steering phrases
    like "не меняй конфигурацию" can never be misread as a denial.
    """
    low = text.strip().lower().strip(" \t.,!?;:")
    if low in _APPROVE_WORDS:
        return True
    if low in _DENY_WORDS:
        return False
    # Denial priority: any standalone deny word in the phrase → deny.
    if any(word in _DENY_WORDS for word in _split_words(low)):
        return False
    first = _split_words(low)[:1]
    if first and first[0] in _APPROVE_WORDS:
        return True
    return None


def _split_words(text: str) -> list[str]:
    """Split into lowercase word tokens, stripping surrounding punctuation."""
    return [tok.strip(" \t.,!?;:()\"'«»") for tok in text.split() if tok.strip(" \t.,!?;:()\"'«»")]


def _filter_approvals_for_flow(approvals: Any, flow_id: str) -> list[Any]:
    """Return only pending approvals that belong to *flow_id*.

    Accepts a plain list of approval entries, a pydantic ``ApprovalListView``
    (``.items`` as a list attribute), or a raw dict response
    (``{"items": [...]}``). Entries may be objects or dicts.
    """
    if approvals is None:
        return []
    if isinstance(approvals, dict):
        items: Any = approvals.get("items", [])
    else:
        entries = getattr(approvals, "items", None)
        if callable(entries):
            items = entries()
        elif entries is not None:
            items = entries
        else:
            items = approvals
    try:
        iterator = iter(items)
    except TypeError:
        return []
    matched: list[Any] = []
    for entry in iterator:
        if isinstance(entry, dict):
            task_id = entry.get("task_id")
        else:
            task_id = getattr(entry, "task_id", None)
        if task_id and str(task_id) == flow_id:
            matched.append(entry)
    return matched


def _first_approval_for_flow(approvals: Any, flow_id: str | None) -> str | None:
    """Pick the pending approval that belongs to *flow_id*.

    Fail-closed: when *flow_id* is known and no entry matches it, returns
    ``None`` — the caller must never decide an unrelated flow's approval.
    The first-entry fallback is used only when *flow_id* is unknown.
    """
    if approvals is None:
        return None
    if flow_id:
        matched = _filter_approvals_for_flow(approvals, flow_id)
        if not matched:
            return None
        first = matched[0]
        if isinstance(first, dict):
            return str(first.get("id", "") or "") or None
        return str(getattr(first, "id", "") or "") or None
    # Unknown flow: first entry of whatever shape we were given.
    if isinstance(approvals, dict):
        items: Any = approvals.get("items", [])
    else:
        entries = getattr(approvals, "items", None)
        if callable(entries):
            items = entries()
        elif entries is not None:
            items = entries
        else:
            items = approvals
    try:
        iterator = iter(items)
    except TypeError:
        return None
    for entry in iterator:
        if isinstance(entry, dict):
            entry_id = str(entry.get("id", "") or "")
        else:
            entry_id = str(getattr(entry, "id", "") or "")
        if entry_id:
            return entry_id
    return None


def _strip_rich_tags(text: str) -> str:
    """Strip Rich style tags (``[dim]``, ``[green]``…) from picker output.

    The approval picker renders Rich-markup strings; in the full-screen layout
    they would appear verbatim.  Only tag-shaped fragments are removed —
    display brackets like ``[ ✅ Одобрить ]`` (space after ``[``) are kept.
    """
    import re

    return re.sub(r"\[(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)?\]", "", text)


class _PickerRendererAdapter:
    """Adapter: picker render_message(role, text) -> ChatController state messages.

    The picker calls ``renderer.render_message(ChatMessageRole.INFO, line)``;
    this adapter appends the line to the live transcript and flushes the
    renderer so the user sees the picker frame immediately.
    """

    def __init__(self, controller: ChatController) -> None:
        self._controller = controller

    def render_message(self, role: ChatMessageRole | str, text: str) -> None:
        if isinstance(role, ChatMessageRole):
            normalized_role = role
        else:
            normalized_role = ChatMessageRole(str(role))
        self._controller.state.messages.append(
            ChatMessage(role=normalized_role, content=_strip_rich_tags(str(text)))
        )
        self._controller._flush_renderer()

    def flush(self) -> None:
        self._controller._flush_renderer()


class ChatController:
    """Live-chat controller — thin client over the canonical Gateway Turn API.

    Stage 1: CLI does NOT own a local DialogueEngine or IntentRouter. Every free
    text turn goes through ``gateway.send_dialogue_turn()`` (the single
    /api/v1/dialogue/turn endpoint owned by the server core). The response_type
    returned by the brain decides the rendering: conversation/clarification →
    show reply; task_accepted → live wait on the returned flow; control/error →
    show reply. If the Gateway is unavailable the controller fails CLOSED with
    an honest message — it never starts a local engine.
    """

    def __init__(
        self,
        gateway: GatewayClientProtocol,
        renderer: RendererProtocol | None = None,
        poll_interval_sec: float = 0.05,
        max_poll_sec: float = 300.0,
        conversation_id: str = "cli-session",
        enable_animations: bool = True,
        dialogue_engine: DialogueEngine | None = None,
        gateway_url: str = "",
        key_bridge: Any = None,
        theme_applier: Any = None,
        theme_custom_applier: Any = None,
    ) -> None:
        self.gateway = gateway
        self.renderer = renderer
        self.poll_interval_sec = poll_interval_sec
        self.max_poll_sec = max_poll_sec
        self.conversation_id = conversation_id
        self.enable_animations = enable_animations
        #: Optional approval-picker key bridge (full-screen layout mode).
        self.key_bridge: Any = key_bridge
        #: Optional callable ``(name: str) -> bool`` that switches the live
        #: layout's colour theme (wired by cli.py to AntigonaLayout.apply_theme).
        self.theme_applier: Any = theme_applier
        #: Optional callable ``(slots: dict) -> bool`` for a custom theme.
        self.theme_custom_applier: Any = theme_custom_applier
        #: Optional callable reporting whether the CLI's PIN owner-mode gate
        #: has been passed for this process (set by cli.py from
        #: AntigonaLayout.owner_mode). None/False means /shell hard-denies.
        self.owner_mode_check: Any = None
        # Stage 1: no local DialogueEngine. The parameter is kept for backward
        # compatibility (tests/shim callers) but the controller never creates or
        # uses a local engine — conversations route through the Gateway.
        self.dialogue_engine: Any = dialogue_engine
        self.state = ChatUIState()
        self.state.is_animating = enable_animations
        self.state.gateway_url = gateway_url
        self.state.session_id = conversation_id
        self.active_flow_id: str | None = None
        self._current_wait_task: asyncio.Task[None] | None = None
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._waiting_flow_id: str | None = None
        self._waiting_approval_id: str | None = None
        self._no_approval_since: float | None = None
        self._last_rendered_index: int = 0
        self._last_rendered_outcome: TerminalOutcome | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._closed: bool = False

    async def refresh_panel_data(self) -> None:
        """Refresh panel fields from the Gateway (flows + approvals + status).

        Best-effort: a Gateway outage must never crash the chat session — the
        panel simply keeps its last known values.  Flows are read from the
        canonical ``list_flows`` and approvals from ``list_approvals``; both are
        already thin-client safe paths in ``GatewayClientProtocol``.
        """
        try:
            flows = await self.gateway.list_flows(limit=20)
            active = [
                flow
                for flow in flows
                if str(
                    getattr(flow, "status", getattr(flow, "state", "UNKNOWN"))
                ).lower()
                not in {"done", "cancelled", "rejected", "failed"}
            ]
            self.state.active_flows = active
            # Rehydrate the panel status from the first real active flow (e.g.
            # after a Gateway restart the monitor must not invent a status).
            if active and self.state.current_status in ("idle", "reconnecting"):
                first_status = str(getattr(active[0], "status", "")).upper()
                mapped = self._TASK_STATUS_MAP.get(first_status)
                if mapped:
                    self.state.current_status = mapped
            elif not active and self.state.current_status in ("failed", "error", "timeout", "cancelled"):
                self.state.current_status = "idle"
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state.active_flows = self.state.active_flows

        try:
            approvals = await self.gateway.list_approvals(status="PENDING", limit=20)
            items = getattr(approvals, "items", approvals)
            self.state.pending_approvals = list(items) if isinstance(items, (list, tuple)) else []
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state.pending_approvals = self.state.pending_approvals

    async def render_initial_panel(self) -> None:
        """Fetch live data and draw the static banner + panel once.

        On TTY the terminal is cleared first so the panel always starts at
        the top of the screen, regardless of the cursor position.
        """
        await self.refresh_panel_data()
        if self.renderer is not None:
            self.renderer.clear_terminal()
            self.renderer.render_panel(self.state)

    async def update_panel(self) -> None:
        """Redraw the static panel after a state change (no animation)."""
        if self.renderer is None:
            return
        await self.refresh_panel_data()
        self.renderer.update_panel(self.state)

    # ── Live agent monitor (Gateway EventLog) ────────────────────────────────
    # A calm background poller over GET /events. It feeds the panel with real,
    # structured flow transitions — never reasoning, never secrets, never a
    # locally invented state. It also drives the reconnect loop: when the
    # Gateway disappears the poller flips the panel to "reconnecting" and keeps
    # retrying with bounded backoff until the connection is restored.

    #: TaskState → panel status mapping (safe display names only).
    _TASK_STATUS_MAP: dict[str, str] = {
        "RECEIVED": "sending",
        "QUEUED": "sending",
        "CREATED": "sending",
        "READY": "planning",
        "PLANNING": "planning",
        "RUNNING": "tool_executing",
        "TOOL_EXECUTING": "tool_executing",
        "OBSERVING": "observing",
        "VERIFYING": "verifying",
        "WAITING_APPROVAL": "waiting_approval",
        "WAITING_USER": "waiting_approval",
        "DONE": "done",
        "FAILED": "failed",
        "BLOCKED": "failed",
        "TIMEOUT": "timeout",
        "POLICY_DENIED": "failed",
        "CANCELLED": "cancelled",
        "RETRY_SCHEDULED": "tool_executing",
    }

    #: Event type labels for the event feed (safe, structured).
    _EVENT_LABELS: dict[str, str] = {
        "PLANNING": "план",
        "RUNNING": "выполнение",
        "TOOL_EXECUTING": "инструмент",
        "OBSERVING": "наблюдение",
        "VERIFYING": "проверка verifier",
        "WAITING_APPROVAL": "требуется подтверждение",
        "WAITING_USER": "требуется подтверждение",
        "DONE": "готово",
        "FAILED": "ошибка",
        "BLOCKED": "заблокировано",
        "TIMEOUT": "таймаут",
        "POLICY_DENIED": "отклонено политикой",
        "CANCELLED": "отменено",
    }

    #: Panel body line budget for events (kept small on narrow terminals).
    _MAX_EVENTS = 6

    async def _events_loop(self) -> None:
        """Background loop: poll Gateway events, update panel, drive reconnect.

        Polls ``GET /events`` with a calm interval.  A Gateway outage never
        kills the session: the loop flips ``state.connection`` to reconnecting,
        stops the flow wait, and retries with bounded backoff until healthy.
        """
        last_seq: int = 0
        backoff = 1.0
        while not self._closed:
            try:
                events = await self.gateway.get_events(after_seq=last_seq, limit=50)
            except asyncio.CancelledError:
                return
            except Exception:
                # Gateway unreachable → reconnecting state + bounded backoff.
                self.state.connection = "reconnecting"
                self.state.current_status = "reconnecting"
                if self._current_wait_task is not None and not self._current_wait_task.done():
                    self._cancel_event.set()
                if self.renderer is not None:
                    self.renderer.update_panel(self.state)
                await asyncio.sleep(min(backoff, 10.0))
                backoff = min(backoff * 1.5, 15.0)
                continue

            backoff = 1.0
            if self.state.connection != "connected":
                # Gateway is back: restore session/flows/approvals state.
                self.state.connection = "connected"
                # Clear the stuck "reconnecting" status; the flow wait (if any)
                # will push the next real transition, and refresh_panel_data
                # rehydrates flows/approvals from the restarted Gateway.
                if self.state.current_status == "reconnecting":
                    self.state.current_status = "idle"
                await self.refresh_panel_data()
                if self.renderer is not None:
                    self.renderer.update_panel(self.state)

            changed = False
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                seq = ev.get("seq", 0)
                # Only consume events we have not seen before (monotonic seq).
                if not isinstance(seq, int) or seq <= last_seq:
                    continue
                last_seq = seq
                to_state = str(ev.get("to_state", "") or ev.get("status", ""))
                if not to_state:
                    continue
                # Map to a safe panel status; unknown states stay as-is.
                status = self._TASK_STATUS_MAP.get(to_state, to_state.lower())
                self.state.current_status = status
                label = self._EVENT_LABELS.get(to_state, to_state.lower())
                flow = str(ev.get("flow_id", ""))[:12]
                feed_item: dict[str, Any] = {
                    "event": label,
                    "detail": f"{flow}…" if flow else "",
                }
                self.state.events.append(feed_item)
                changed = True

            if changed:
                # Keep the feed bounded; never store raw payloads.
                self.state.events = self.state.events[-self._MAX_EVENTS:]
                if self.renderer is not None:
                    self.renderer.update_panel(self.state)

            await asyncio.sleep(0.8)

    async def start_monitor(self) -> None:
        """Start the background event-monitor loop (idempotent)."""
        if self._events_task is not None and not self._events_task.done():
            return
        self._closed = False
        self._events_task = asyncio.create_task(self._events_loop())

    async def stop_monitor(self) -> None:
        """Stop the background event-monitor loop."""
        self._closed = True
        if self._events_task is not None and not self._events_task.done():
            self._events_task.cancel()
            try:
                await self._events_task
            except (asyncio.CancelledError, Exception):
                pass
            self._events_task = None

    def _flush_renderer(self) -> None:
        if self.renderer is not None:
            while self._last_rendered_index < len(self.state.messages):
                msg = self.state.messages[self._last_rendered_index]
                self.renderer.render_message(msg.role, msg.content, width=self.state.width)
                self._last_rendered_index += 1

            if (
                self.state.terminal_outcome is not None
                and self.state.terminal_outcome is not self._last_rendered_outcome
            ):
                self.renderer.render_outcome(self.state.terminal_outcome)
                self._last_rendered_outcome = self.state.terminal_outcome

    async def handle_input(self, raw_input: str) -> CommandDisposition:
        """Parse raw input, dispatch typed commands, or fail closed for generic text."""
        # Expand a leading alias token (e.g. /s -> /status) before parsing.
        from antigona.cli_ui import aliases
        raw_input = aliases.expand(raw_input)

        parsed = parse_command(raw_input)
        if parsed.kind == CommandKind.NOOP:
            return CommandDisposition.LOCAL_ACTION

        self.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content=raw_input))

        # Structural malformed check: embedded newlines or NUL bytes in slash commands fail closed
        stripped = raw_input.strip()
        if stripped.startswith("/") and ("\n" in raw_input or "\r" in raw_input or "\0" in raw_input):
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.REJECTED,
                error_message="Slash commands with embedded newlines or NUL bytes are malformed and fail closed.",
            )
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED

        # ── Structural guard: any slash-prefixed input stays local ─────────────
        # Never, ever send a slash command to POST /tasks.
        is_slash = raw_input.lstrip().startswith("/")

        # Unknown slash command: local error with suggestions, never hits Gateway
        if parsed.kind == CommandKind.UNKNOWN:
            if raw_input.strip() == "/":
                # Slash menu: user typed just "/" — list every command.
                from antigona.cli_ui.prompts import DEFAULT_SLASH_COMMANDS

                lines = ["📋 Команды"]
                for cmd in DEFAULT_SLASH_COMMANDS:
                    lines.append(f"  {cmd.name} {cmd.description}")
                lines.append("  /… — автодополнение: Tab/стрелки")
                self.state.messages.append(
                    ChatMessage(role=ChatMessageRole.INFO, content="\n".join(lines))
                )
                self._flush_renderer()
                return CommandDisposition.LOCAL_ACTION
            suggestions = _slash_suggestions(parsed.command_name)
            self.state.messages.append(ChatMessage(role=ChatMessageRole.ERROR, content=suggestions))
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        # Malformed known command: show usage for that specific command
        if parsed.kind == CommandKind.MALFORMED:
            usage = _usage_for(parsed.command_name)
            self.state.messages.append(ChatMessage(role=ChatMessageRole.ERROR, content=usage))
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        # Owner-elevated host shell: fully local, never touches the Gateway/LLM.
        # Only reachable by a human typing /shell at this exact prompt — the
        # model has no tool-call path to this, so prompt injection from web
        # content, file contents, or a task description cannot trigger it.
        if parsed.kind == CommandKind.SHELL:
            return await self._handle_shell_command(parsed.args[0] if parsed.args else "")

        # Provider switch / model select: canonical runtime state via the resolver.
        if parsed.kind in (CommandKind.PROVIDER, CommandKind.SETLLM):
            return await self._handle_provider_command(parsed.args)
        if parsed.kind == CommandKind.MODEL:
            return await self._handle_model_command(parsed.args)

        # Local read-only/system commands (never touch the Gateway or LLM).
        if parsed.kind == CommandKind.UPTIME:
            return await self._handle_uptime()
        if parsed.kind == CommandKind.EXPORT:
            return await self._handle_export(parsed.args)
        if parsed.kind == CommandKind.THEME:
            return await self._handle_theme(parsed.args)
        if parsed.kind == CommandKind.ALIAS:
            return await self._handle_alias(parsed.args)
        if parsed.kind == CommandKind.SESSIONS:
            return await self._handle_sessions(parsed.args)

        # Informational command intents (/model, /providers, /bot) go to the
        # conversation handler via the dialogue turn (NOT /tasks) — the server
        # brain answers about the current model, providers, and bot. This keeps
        # the CLI contract aligned with the backend routing (d9036955).
        if parsed.kind == CommandKind.INFORMATIONAL:
            return await self._handle_free_text(raw_input)

        # Server-brain command intents (/install, /mcp, /plugins, /cli) — the
        # CLI is a THIN client: it forwards the raw slash command to the server
        # brain via the dialogue turn, which classifies (command.install /
        # command.mcp / command.plugins / command.cli) and answers
        # deterministically. Never handled locally, never submit_task.
        if parsed.kind in (
            CommandKind.INSTALL,
            CommandKind.MCP,
            CommandKind.PLUGINS,
            CommandKind.SKILLS,
            CommandKind.CLI,
        ):
            return await self._handle_free_text(raw_input)

        # Slash-prefixed input that the parser couldn't classify — reject locally.
        # This is the structural guarantee: any /something that escaped the UNKNOWN
        # and MALFORMED checks (e.g. a future parser regression) never reaches
        # submit_task.  With the current parser every known verb returns UNKNOWN,
        # MALFORMED, or a typed command kind, so this is a safety net.
        if is_slash and parsed.kind == CommandKind.UNSUPPORTED:
            suggestions = _slash_suggestions(parsed.command_name or raw_input.strip().split()[0])
            self.state.messages.append(ChatMessage(role=ChatMessageRole.ERROR, content=suggestions))
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        # ── Non-slash free text only below this line ───────────────────────────
        if parsed.kind == CommandKind.UNSUPPORTED:
            # Live-chat turn layer: natural approval, intent routing, chit-chat,
            # steering/control, and task submission with a live wait.
            return await self._handle_free_text(raw_input)

        # Dispatch through Checkpoint 4 safe command dispatcher
        try:
            res = await dispatch_command(parsed, self.gateway)
        except Exception:
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.ERROR,
                error_message="Gateway request failed.",
            )
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED

        if res.disposition == CommandDisposition.LOCAL_ACTION:
            if parsed.kind == CommandKind.HELP:
                if res.data is not None:
                    self.state.messages.append(
                        ChatMessage(role=ChatMessageRole.INFO, content=str(res.data))
                    )
            elif parsed.kind == CommandKind.LIST:
                self.state.messages.append(
                    render_flow_list(res.data if isinstance(res.data, (list, tuple)) else [])
                )
            elif parsed.kind == CommandKind.APPROVALS:
                self.state.messages.append(render_approval_list(res.data))
            elif parsed.kind == CommandKind.REDRAW:
                if self.renderer is not None:
                    self.renderer.clear_and_repaint(self.state)
            self._flush_renderer()
            return res.disposition

        if res.disposition == CommandDisposition.GATEWAY_EXECUTION:
            if parsed.kind in (CommandKind.GET, CommandKind.STATUS) and len(parsed.args) == 1:
                self.active_flow_id = parsed.args[0]
                await self.poll_active_flow(self.active_flow_id)
            elif parsed.kind in (CommandKind.APPROVE, CommandKind.DENY):
                if len(parsed.args) == 1:
                    self.state.messages.append(
                        ChatMessage(
                            role=ChatMessageRole.INFO,
                            content=f"Decision '{parsed.command_name}' sent for {parsed.args[0]}",
                        )
                    )
                elif isinstance(res.data, dict) and res.data.get("message"):
                    self.state.messages.append(
                        ChatMessage(
                            role=ChatMessageRole.INFO,
                            content=str(res.data["message"]),
                        )
                    )
                self._waiting_approval_id = None
                # Live-chat behaviour: after a decision, resume watching the flow
                # that was waiting for it, until it completes or blocks again.
                # Only the flow that actually returned from the wait loop at
                # WAITING_APPROVAL is resumed (never a flow the user navigated
                # to via /status or /get), and the resume is guarded so a
                # routine wait failure cannot kill the whole session.
                if self._waiting_flow_id is not None:
                    try:
                        await self._poll_flow_until_terminal_or_approval(self._waiting_flow_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.state.terminal_outcome = TerminalOutcome(
                            status=TerminalOutcomeStatus.ERROR,
                            error_message="Gateway request failed; details withheld: [REDACTED]",
                        )
                        self.state.messages.append(render_outcome(self.state.terminal_outcome))
                        self._flush_renderer()
            elif parsed.kind == CommandKind.CANCEL:
                self.state.messages.append(
                    ChatMessage(
                        role=ChatMessageRole.INFO, content=f"Cancel requested for {parsed.args[0]}"
                    )
                )
            elif parsed.kind == CommandKind.LIST:
                self.state.messages.append(
                    render_flow_list(res.data if isinstance(res.data, (list, tuple)) else [])
                )
            elif parsed.kind == CommandKind.STATUS and not parsed.args:
                self.state.messages.append(
                    render_flow_list(res.data if isinstance(res.data, (list, tuple)) else [])
                )
            elif parsed.kind == CommandKind.APPROVALS:
                self.state.messages.append(render_approval_list(res.data))
            elif parsed.kind == CommandKind.HEALTH:
                self.state.messages.append(render_health(res.data))
            elif parsed.kind == CommandKind.COMMANDS:
                self.state.messages.append(render_commands_list(res.data))
            elif parsed.kind == CommandKind.MEMORY:
                self.state.messages.append(render_memory_list(res.data))
            elif parsed.kind == CommandKind.SESSION:
                self.state.messages.append(render_session_info(res.data))
            elif parsed.kind == CommandKind.HISTORY:
                self.state.messages.append(render_session_history(res.data))
            elif res.data is not None:
                self.state.messages.append(render_flow_status(res.data))

            self._flush_renderer()
            return res.disposition

        if res.disposition == CommandDisposition.ERROR:
            error_msg = res.error or "Gateway request failed."
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.ERROR,
                error_message=error_msg,
            )
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED

        # Fail closed for error / unrecognized
        self.state.terminal_outcome = TerminalOutcome(
            status=TerminalOutcomeStatus.REJECTED,
            error_message="Gateway request failed.",
        )
        self.state.messages.append(render_outcome(self.state.terminal_outcome))
        self._flush_renderer()
        return CommandDisposition.FAIL_CLOSED

    
    async def _handle_uptime(self) -> CommandDisposition:
        """``/uptime`` — read-only host system info. Pure local, no Gateway/LLM.

        Reads live data from /proc (uptime, load, memory) and shutil/psutil-free
        counters, so it never depends on external services or network.
        """
        self.state.messages.append(
            ChatMessage(role=ChatMessageRole.INFO, content=_system_info_text())
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    async def _handle_export(self, args: tuple[str, ...]) -> CommandDisposition:
        """``/export [path]`` — write the current transcript to a markdown file.

        Default path is ``~/antigona_chat_<session>_<ts>.md``. A user-supplied
        path is expanded and bounded so the file cannot escape the owner's home
        (rejects absolute paths and ``..`` traversal). Pure local write.
        """
        try:
            out_path = _export_transcript(self.state, args[0] if args else "")
        except ValueError as exc:
            self.state.messages.append(
                ChatMessage(role=ChatMessageRole.ERROR, content=str(exc))
            )
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED
        self.state.messages.append(
            ChatMessage(
                role=ChatMessageRole.INFO,
                content=f"💾 Диалог экспортирован: {out_path}",
            )
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    
    async def _handle_theme(self, args: tuple[str, ...]) -> CommandDisposition:
        """``/theme [name]`` — show or switch the terminal colour theme.

        No args → report the active theme and list every registered theme.
        ``/theme <name>`` → switch the live layout (via ``theme_applier``)
        and persist it. Unknown name → fail-closed error with the list.
        """
        from antigona.cli_ui import themes

        if args and args[0] == "custom":
            return await self._handle_theme_custom(args[1:])
        if args and args[0] == "save":
            return await self._handle_theme_save(args[1:])

        if not args:
            active = themes.get_active()
            avail = ", ".join(f"{t.name}" for t in themes.list_themes())
            self.state.messages.append(
                ChatMessage(
                    role=ChatMessageRole.INFO,
                    content=f"🎨 Тема: {active.name} ({active.label})\nДоступно: {avail}\nСменить: /theme <name>",
                )
            )
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        name = args[0]
        if self.theme_applier is not None:
            ok = self.theme_applier(name)
        else:
            try:
                themes.set_active(name)
                ok = True
            except KeyError:
                ok = False
        if not ok:
            avail = ", ".join(t.name for t in themes.list_themes())
            self.state.messages.append(
                ChatMessage(
                    role=ChatMessageRole.ERROR,
                    content=f"Неизвестная тема: {name}. Доступно: {avail}",
                )
            )
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED
        applied = themes.get_active()
        self.state.messages.append(
            ChatMessage(
                role=ChatMessageRole.INFO,
                content=f"🎨 Тема: {applied.name} ({applied.label})",
            )
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    
    async def _handle_alias(self, args: tuple[str, ...]) -> CommandDisposition:
        """``/alias`` — list, define or delete command shortcuts.

        No args → list aliases. ``/alias --del <name>`` → delete.
        ``/alias <name> <command...>`` → set/overwrite. Pure local (JSON).
        """
        from antigona.cli_ui import aliases

        if not args:
            current = aliases.load_aliases()
            if not current:
                body = "🔗 Алиасы: пока нет. Создать: /alias <имя> <команда>"
            else:
                rows = "\n".join(f"  /{k}  →  {v}" for k, v in sorted(current.items()))
                body = f"🔗 Алиасы:\n{rows}"
            self.state.messages.append(ChatMessage(role=ChatMessageRole.INFO, content=body))
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        if args[0] == "--del":
            removed = aliases.delete_alias(args[1])
            body = f"🗑 Алиас /{args[1]} удалён." if removed else f"Алиас /{args[1]} не найден."
            self.state.messages.append(
                ChatMessage(role=ChatMessageRole.ERROR if not removed else ChatMessageRole.INFO,
                            content=body)
            )
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        name, command = args[0], args[1]
        try:
            aliases.set_alias(name, command)
        except ValueError as exc:
            self.state.messages.append(ChatMessage(role=ChatMessageRole.ERROR, content=str(exc)))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED
        self.state.messages.append(
            ChatMessage(role=ChatMessageRole.INFO, content=f"🔗 Алиас: /{name} → {command}")
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    
    async def _handle_sessions(self, args: tuple[str, ...]) -> CommandDisposition:
        """``/sessions`` — list the most recent conversation sessions (local DB).

        Reads the canonical sessions SQLite store (``core.paths.sessions_db_path``)
        through the SessionDatabase API. Informational and read-only; missing or
        unreadable DB yields an empty, honest result.
        """
        from antigona.core.paths import sessions_db_path
        from antigona.sessions.database import SessionDatabase

        rows: list[dict[str, Any]] = []
        try:
            async with SessionDatabase(str(sessions_db_path())) as db:
                rows = await db.list_sessions(limit=15)
        except Exception:
            rows = []

        if not rows:
            body = "💬 Сессий пока нет."
        else:
            lines = ["💬 Недавние сессии:"]
            for r in rows:
                sid = str(r.get("id", ""))
                title = str(r.get("title", "") or "")[:38]
                status = str(r.get("status", ""))
                updated = str(r.get("updated_at", ""))[:19]
                lines.append(f"  {sid}  [{status}]  {updated}  {title}".rstrip())
            body = "\n".join(lines)
        self.state.messages.append(ChatMessage(role=ChatMessageRole.INFO, content=body))
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    
    async def _handle_theme_custom(self, custom_args: tuple[str, ...]) -> CommandDisposition:
        """``/theme custom <hex...>`` — build a fully custom colour theme.

        Positional hex values map to ``themes.CUSTOM_SLOT_ORDER`` (accent,
        violet, magenta, amber, blue, red, bg, fg, header_bg, menu_bg,
        menu_fg). No args → show the slot order/usage.
        """
        from antigona.cli_ui import themes

        if not custom_args:
            order = " ".join(themes.CUSTOM_SLOT_ORDER)
            self.state.messages.append(
                ChatMessage(
                    role=ChatMessageRole.INFO,
                    content=(
                        "🎨 /theme custom — свой дизайн.\n"
                        f"Порядок цветов: {order}\n"
                        "Пример: /theme custom #00FFCC #FF2BD6 #241B36"
                    ),
                )
            )
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        try:
            slots = themes.parse_custom_args(custom_args)
        except ValueError as exc:
            self.state.messages.append(ChatMessage(role=ChatMessageRole.ERROR, content=str(exc)))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED

        if self.theme_custom_applier is not None:
            self.theme_custom_applier(slots)
        else:
            themes.set_custom(slots)
        applied = themes.get_active()
        self.state.messages.append(
            ChatMessage(role=ChatMessageRole.INFO, content=f"🎨 Кастомная тема применена ({applied.label}).")
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    
    async def _handle_theme_save(self, save_args: tuple[str, ...]) -> CommandDisposition:
        """``/theme save <name>`` — save the active theme's colours as a named theme."""
        from antigona.cli_ui import themes

        if not save_args:
            self.state.messages.append(
                ChatMessage(role=ChatMessageRole.ERROR, content="Укажите имя: /theme save <имя>")
            )
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED
        name = save_args[0]
        active = themes.get_active()
        slots = {slot: getattr(active, slot) for slot in themes.CUSTOM_SLOT_ORDER}
        try:
            saved = themes.save_user_theme(name, slots)
        except ValueError as exc:
            self.state.messages.append(ChatMessage(role=ChatMessageRole.ERROR, content=str(exc)))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED
        # repaint the live layout with the saved theme
        if self.theme_applier is not None:
            self.theme_applier(saved.name)
        self.state.messages.append(
            ChatMessage(
                role=ChatMessageRole.INFO,
                content=f"🎨 Тема сохранена: /{saved.name} ({saved.label})",
            )
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    async def _handle_shell_command(self, command: str) -> CommandDisposition:
        """Run /shell locally as the elevated owner via UnifiedToolExecutionLayer.

        Denies closed whenever ``owner_mode_check`` is unset, returns False,
        or raises — a session that never passed the CLI PIN gate (or whose
        attempts were exhausted) must not run anything.
        """
        from antigona.engine.unified_executor import (
            OwnerAuthContext,
            ToolExecutionRequest,
            UnifiedToolExecutionLayer,
        )
        from antigona.observability_legacy import record as _legacy_record

        _legacy_record("cli_ui.chat.local_shell_exec")

        try:
            is_owner = bool(self.owner_mode_check and self.owner_mode_check())
        except Exception:
            is_owner = False

        # G-02b / CP-2: PIN state comes from the shared elevation authority, not
        # from re-using is_owner. The CLI PIN gate records elevation there on
        # success (layout._grant_owner_mode); a fail-closed miss denies /shell.
        try:
            from antigona.security.elevation import (
                CLI_OWNER_PRINCIPAL,
                owner_elevation_authority,
            )

            pin_verified = owner_elevation_authority().is_elevated(CLI_OWNER_PRINCIPAL)
        except Exception:
            pin_verified = False

        auth = OwnerAuthContext(is_owner=is_owner, pin_verified=pin_verified)
        unified = UnifiedToolExecutionLayer()

        req = ToolExecutionRequest(
            tool_name="run_shell",
            params={"command": command},
            requester="owner",
            channel="cli",
            user_id=cli_principal(),
            session_id=self.conversation_id,
            correlation_id=f"cli-shell-{int(time.monotonic())}",
            owner_auth=auth,
        )

        try:
            res_json = await unified.execute(req)
            res_dict = json.loads(res_json)
        except Exception as exc:
            self.state.messages.append(
                ChatMessage(
                    role=ChatMessageRole.ERROR,
                    content=f"❌ /shell failed: {type(exc).__name__}: {exc}",
                )
            )
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED

        if "error" in res_dict:
            self.state.messages.append(
                ChatMessage(role=ChatMessageRole.ERROR, content=f"🔒 {res_dict['error']}")
            )
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED

        exit_code = res_dict.get("exit_code", 0)
        output = res_dict.get("output", "")
        lines = [f"$ {command}", f"[exit {exit_code}]"]
        if output:
            lines.append(output.rstrip("\n"))

        self.state.messages.append(
            ChatMessage(
                role=ChatMessageRole.INFO if exit_code == 0 else ChatMessageRole.ERROR,
                content="\n".join(lines),
            )
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    async def handle_user_input(
        self, user_prompt: str, metadata: dict[str, Any] | None = None
    ) -> CommandDisposition:
        """Alias method for input handling."""
        return await self.handle_input(user_prompt)

    async def handle_control_command(self, raw_input: str) -> CommandDisposition:
        """Alias method for control command handling."""
        return await self.handle_input(raw_input)

    async def cancel_local_wait(self) -> None:
        """Cancel local polling task locally; NEVER POSTs cancel to Gateway."""
        self._cancel_event.set()
        if self._current_wait_task is not None and not self._current_wait_task.done():
            self._current_wait_task.cancel()
            try:
                await self._current_wait_task
            except asyncio.CancelledError:
                pass
            self._current_wait_task = None

        self.state.terminal_outcome = TerminalOutcome(
            status=TerminalOutcomeStatus.CANCELLED,
            error_message="Local wait was cancelled.",
        )
        self.state.current_status = "cancelled"
        self.state.messages.append(render_outcome(self.state.terminal_outcome))
        self._flush_renderer()

    @staticmethod
    def _outcome_from_flow_view(flow_view: Any) -> TerminalOutcome:
        """Translate a flow view through the validated boundary adapter."""
        status = getattr(flow_view, "status", None)
        result_data = getattr(flow_view, "result", getattr(flow_view, "result_data", None))
        error_msg = getattr(flow_view, "error", getattr(flow_view, "error_message", None))
        return adapt_flow_status(status, result_data=result_data, error_message=error_msg)

    async def _await_canonical_terminal(self, waiter: TerminalWaiterProtocol, flow_id: str) -> None:
        """Wait through the canonical waiter — the only path that may yield SUCCESS."""
        flow_view = await waiter.wait_for_terminal(
            flow_id,
            timeout=self.max_poll_sec,
            poll_interval=self.poll_interval_sec,
            cancel_event=self._cancel_event,
        )
        outcome = self._outcome_from_flow_view(flow_view)
        self.state.terminal_outcome = outcome
        self.state.current_status = "idle"
        self.state.messages.append(render_outcome(outcome))
        self._flush_renderer()

    async def _poll_without_canonical_waiter(self, flow_id: str) -> None:
        """Bounded ``get_flow`` poll for clients that expose no canonical waiter.

        This path reports non-success lifecycle states and hands control back locally
        when the flow blocks on a human decision.  It never opens the success gate: a
        flow reporting DONE here has not been validated by
        ``GatewayClient.wait_for_terminal()``, so it is surfaced as MALFORMED.
        """
        deadline = time.monotonic() + self.max_poll_sec
        while True:
            if self._cancel_event.is_set():
                raise asyncio.CancelledError
            if time.monotonic() > deadline:
                raise TimeoutError

            try:
                flow_view = await self.gateway.get_flow(flow_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fixed message only — gateway exception text may embed credentials.
                self.state.terminal_outcome = TerminalOutcome(
                    status=TerminalOutcomeStatus.ERROR,
                    error_message="Gateway request failed; details withheld: [REDACTED]",
                )
                self.state.messages.append(render_outcome(self.state.terminal_outcome))
                self._flush_renderer()
                return

            outcome = self._outcome_from_flow_view(flow_view)

            if outcome.status is TerminalOutcomeStatus.WAITING_APPROVAL:
                self.state.current_status = "waiting_approval"
                approvals: Any = None
                try:
                    approvals = await self.gateway.list_approvals(status="PENDING", limit=500)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    approvals = None
                flow_approvals = _filter_approvals_for_flow(approvals, flow_id)
                self._waiting_approval_id = _first_approval_for_flow(flow_approvals, flow_id)
                if flow_approvals:
                    self.state.messages.append(render_approval_list(flow_approvals))
                else:
                    self.state.messages.append(
                        ChatMessage(
                            role=ChatMessageRole.INFO,
                            content="Ожидается решение для задачи…",
                        )
                    )
                self._flush_renderer()
                return

            if outcome.status in (TerminalOutcomeStatus.ACCEPTED, TerminalOutcomeStatus.QUEUED):
                self.state.current_status = "running"
                self._flush_renderer()
                await asyncio.sleep(self.poll_interval_sec)
                continue

            if outcome.status is TerminalOutcomeStatus.SUCCESS:
                outcome = TerminalOutcome(
                    status=TerminalOutcomeStatus.MALFORMED,
                    error_message=(
                        "Terminal success was not validated by the canonical "
                        "GatewayClient.wait_for_terminal(); refusing to present success."
                    ),
                )

            self.state.terminal_outcome = outcome
            self.state.current_status = "idle"
            self.state.messages.append(render_outcome(outcome))
            self._flush_renderer()
            return

    # ── Live-chat turn layer ───────────────────────────────────────────────────
    # Every free-text input is a *turn*: natural approval first, then the single
    # server Turn API. Stage 1: the CLI is a THIN client — it has no local
    # IntentRouter/DialogueEngine; classification and routing happen in the
    # server core. The brain's response_type decides rendering. If the Gateway
    # is unavailable the controller fails CLOSED (honest message, no local
    # engine, no task execution).


    async def _handle_provider_command(self, args: tuple[str, ...]) -> CommandDisposition:
        """Handle /provider or /setllm commands for listing or switching LLM providers."""
        from antigona.tools.provider_switcher import (
            get_available_providers,
            set_active_model,
            switch_to_provider,
        )

        if not args:
            providers = get_available_providers()
            lines = ["🏭 **Доступные LLM-провайдеры Antigona**:\n"]
            for p in providers:
                lines.append(f"• **{p['display_name']}** (`{p['name']}`): {p['status']} (модель: `{p['model']}`)")
            lines.append("\n💡 *Использование*: `/provider <имя>` или `/setllm <имя> [модель]`")
            lines.append("   *Пример*: `/setllm ollama` или `/setllm deepseek`")
            self.state.messages.append(ChatMessage(role=ChatMessageRole.INFO, content="\n".join(lines)))
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        target_provider = args[0].lower().strip()
        model_name = args[1].strip() if len(args) >= 2 else None
        success, msg = switch_to_provider(target_provider)
        if success and model_name:
            set_active_model(target_provider, model_name)
            msg += f" (установлена модель: `{model_name}`)"

        role = ChatMessageRole.INFO if success else ChatMessageRole.ERROR
        self.state.messages.append(ChatMessage(role=role, content=msg))
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    async def _handle_model_command(self, args: tuple[str, ...]) -> CommandDisposition:
        """Handle /model command for viewing or setting the active model."""
        from antigona.tools.provider_switcher import (
            get_active_runtime_info,
            set_active_model,
        )

        info = get_active_runtime_info()

        if not args:
            if info.status != "active":
                msg = (
                    "🤖 **Текущая модель Antigona**:\n"
                    "• Провайдер: `runtime provider unresolved`\n\n"
                    "💡 *Использование*: `/model <имя_модели>` или `/setllm <провайдер> <модель>`"
                )
            else:
                msg = (
                    f"🤖 **Текущая модель Antigona**:\n"
                    f"• Провайдер: `{info.display_name}` (`{info.provider_name}`)\n"
                    f"• Активная модель: `{info.model_name}`\n"
                    f"• Endpoint class: `{info.endpoint_class}`\n\n"
                    f"💡 *Использование*: `/model <имя_модели>` или `/setllm <провайдер> <модель>`"
                )
            self.state.messages.append(ChatMessage(role=ChatMessageRole.INFO, content=msg))
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION

        new_model = args[0].strip()
        current_prov = info.provider_name if info.status == "active" else "ollama"
        success, _ = set_active_model(current_prov, new_model)

        msg = f"✅ Модель обновлена: `{new_model}` (для провайдера `{current_prov}`)."
        self.state.messages.append(ChatMessage(role=ChatMessageRole.INFO, content=msg))
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION



    async def _handle_free_text(self, text: str) -> CommandDisposition:
        stripped = text.strip()
        if not stripped:
            return CommandDisposition.LOCAL_ACTION

        # 1) Natural-language approval while a decision is pending for the flow
        #    the CLI is waiting on — no IDs in the happy path.
        if self._waiting_approval_id is not None:
            decision = _approval_word_decision(stripped)
            if decision is not None:
                return await self._decide_pending_approval(decision, stripped)

        # 2) Single Turn API call — the server brain classifies and routes.
        turn_id = f"cli:turn:{uuid.uuid4().hex}"
        try:
            res = await self.gateway.send_dialogue_turn(
                text=stripped,
                session_id=self.conversation_id,
                channel="cli",
                user_id=cli_principal(),
                turn_id=turn_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Fail CLOSED: Gateway unavailable → honest message, no local engine.
            logger.warning("send_dialogue_turn failed for CLI session: %s", exc)
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.ERROR,
                error_message=(
                    "Gateway недоступен. Свободный диалог и задачи временно "
                    "недоступны, пока Gateway не запущен."
                ),
            )
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED

        response_type = str(res.get("response_type", "conversation"))
        reply = str(res.get("reply", "") or "")

        # 3) Task accepted → the server core created the flow; live wait on it.
        if response_type == "task_accepted":
            flow_id = res.get("flow_id")
            if not flow_id:
                self.state.messages.append(
                    ChatMessage(
                        role=ChatMessageRole.ERROR,
                        content="Задача принята, но flow_id отсутствует в ответе ядра.",
                    )
                )
                self._flush_renderer()
                return CommandDisposition.FAIL_CLOSED
            flow_id_str = str(flow_id)
            self.active_flow_id = flow_id_str
            self.state.last_event = f"task accepted: {flow_id_str[:12]}…"
            self.state.messages.append(
                ChatMessage(
                    role=ChatMessageRole.SYSTEM,
                    content=f"Task created: ID={flow_id_str[:12]}...",
                )
            )
            self._flush_renderer()
            await self.update_panel()
            await self._poll_flow_until_terminal_or_approval(flow_id_str)
            return CommandDisposition.GATEWAY_EXECUTION

        # 4) Conversation / clarification / control / error → render the reply.
        self.state.messages.append(
            ChatMessage(role=ChatMessageRole.ASSISTANT, content=reply or "...")
        )
        self._flush_renderer()
        return CommandDisposition.LOCAL_ACTION

    async def _decide_pending_approval(self, approve: bool, raw: str) -> CommandDisposition:
        approval_id = self._waiting_approval_id
        self._waiting_approval_id = None
        if not approval_id:
            self.state.messages.append(
                ChatMessage(
                    role=ChatMessageRole.ERROR,
                    content="Не найден ожидающий approval для активной задачи.",
                )
            )
            self._flush_renderer()
            return CommandDisposition.LOCAL_ACTION
        try:
            await self.gateway.decide_approval(approval_id, approve=approve)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.ERROR,
                error_message="Gateway request failed; details withheld: [REDACTED]",
            )
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
            return CommandDisposition.FAIL_CLOSED
        verb = "одобрено" if approve else "отклонено"
        self.state.messages.append(
            ChatMessage(
                role=ChatMessageRole.INFO,
                content=f"Решение: {verb} ({approval_id[:8]}…)",
            )
        )
        self._flush_renderer()
        if self._waiting_flow_id is not None:
            try:
                await self._poll_flow_until_terminal_or_approval(self._waiting_flow_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.state.terminal_outcome = TerminalOutcome(
                    status=TerminalOutcomeStatus.ERROR,
                    error_message="Gateway request failed; details withheld: [REDACTED]",
                )
                self.state.messages.append(render_outcome(self.state.terminal_outcome))
                self._flush_renderer()
        return CommandDisposition.GATEWAY_EXECUTION

    async def _poll_flow_until_terminal_or_approval(self, flow_id: str) -> None:
        """Live-chat wait: watch a flow until it completes or blocks on a decision.

        Restores the pre-74da4b59 behaviour for free text: after submission the
        CLI keeps polling the flow instead of returning to the prompt.  When the
        flow blocks on a human decision (``WAITING_APPROVAL`` / ``WAITING_USER``)
        the pending approvals are rendered and control returns so the user can
        ``/approve`` or ``/deny``; the decision branch then resumes this loop via
        ``self._waiting_flow_id``.  A terminal ``DONE`` is always validated
        through the canonical ``wait_for_terminal`` (the only path allowed to
        open the success gate), never through a bare ``get_flow``.
        """
        if not flow_id:
            return
        # A previous cancel_local_wait() must never disable this feature: each
        # wait starts with a fresh event.
        self._cancel_event.clear()
        task = asyncio.create_task(self._poll_flow_until_terminal_or_approval_inner(flow_id))
        self._current_wait_task = task
        # Surface the in-flight wait in the persistent status bar.
        from antigona.cli_ui.activity import get_tracker

        wait_act = get_tracker().start("process", "Waiting for flow", detail=f"ID={flow_id[:12]}...")
        try:
            # While the wait runs the prompt is not being read, so a real Ctrl+C
            # arrives as SIGINT. Convert it into a local wait cancellation instead
            # of letting it kill the whole session (cli.py would swallow it and exit).
            loop = asyncio.get_running_loop()
            signal_installed = False
            try:
                loop.add_signal_handler(signal.SIGINT, self._cancel_current_wait)
                signal_installed = True
            except (NotImplementedError, RuntimeError):
                # Non-main thread / unsupported platform: fall back to default SIGINT.
                signal_installed = False
            try:
                if (
                    self.enable_animations
                    and self.renderer is not None
                    and hasattr(self.renderer, "live_spinner")
                ):
                    with self.renderer.live_spinner(
                        status_text=_phase_text(self.state.current_status)
                    ):
                        await task
                else:
                    await task
            finally:
                if signal_installed:
                    loop.remove_signal_handler(signal.SIGINT)
                self._current_wait_task = None
        finally:
            get_tracker().finish(wait_act)

    def _cancel_current_wait(self) -> None:
        """Signal-handler target: cancel the in-flight wait, keep the session."""
        self._cancel_event.set()
        task = self._current_wait_task
        if task is not None and not task.done():
            task.cancel()

    async def _poll_flow_until_terminal_or_approval_inner(self, flow_id: str) -> None:
        deadline = time.monotonic() + self.max_poll_sec
        # Fresh grace timer for every wait: a stale timestamp from a previous
        # cancelled/timed-out/errored wait must never expire the next window.
        self._no_approval_since = None
        waiter: TerminalWaiterProtocol | None = (
            self.gateway if isinstance(self.gateway, TerminalWaiterProtocol) else None
        )
        try:
            while True:
                if self._cancel_event.is_set():
                    self._waiting_flow_id = None
                    self._waiting_approval_id = None
                    self.state.terminal_outcome = TerminalOutcome(
                        status=TerminalOutcomeStatus.CANCELLED,
                        error_message="Local wait was cancelled.",
                    )
                    self.state.current_status = "cancelled"
                    self.state.messages.append(render_outcome(self.state.terminal_outcome))
                    self._flush_renderer()
                    return
                if time.monotonic() > deadline:
                    self._waiting_flow_id = None
                    self._waiting_approval_id = None
                    self.state.terminal_outcome = TerminalOutcome(
                        status=TerminalOutcomeStatus.TIMEOUT,
                        error_message="Local wait timed out.",
                    )
                    self.state.current_status = "timeout"
                    self.state.messages.append(render_outcome(self.state.terminal_outcome))
                    self._flush_renderer()
                    return

                try:
                    flow_view = await self.gateway.get_flow(flow_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._waiting_flow_id = None
                    self._waiting_approval_id = None
                    self.state.terminal_outcome = TerminalOutcome(
                        status=TerminalOutcomeStatus.ERROR,
                        error_message="Gateway request failed; details withheld: [REDACTED]",
                    )
                    self.state.messages.append(render_outcome(self.state.terminal_outcome))
                    self._flush_renderer()
                    return

                outcome = self._outcome_from_flow_view(flow_view)

                if outcome.status is TerminalOutcomeStatus.WAITING_APPROVAL:
                    try:
                        approvals = await self.gateway.list_approvals(status="PENDING", limit=500)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        approvals = None
                    flow_approvals = _filter_approvals_for_flow(approvals, flow_id)
                    if not flow_approvals:
                        # The flow blocks on a decision but the approval row is
                        # not materialised yet (worker is mid-transition). Keep
                        # polling for a short grace window instead of dropping
                        # the user back to the prompt with nothing to decide.
                        now = time.monotonic()
                        if self._no_approval_since is None:
                            self._no_approval_since = now
                        if now - self._no_approval_since < _APPROVAL_GRACE_SECONDS:
                            self.state.current_status = "running"
                            self._flush_renderer()
                            await asyncio.sleep(self.poll_interval_sec)
                            continue
                        self._no_approval_since = None
                        self._waiting_flow_id = flow_id
                        self.state.current_status = "waiting_approval"
                        self.state.last_event = f"ожидается решение для {flow_id[:12]}…"
                        self.state.messages.append(
                            ChatMessage(
                                role=ChatMessageRole.INFO,
                                content="Ожидается решение для задачи…",
                            )
                        )
                        self._flush_renderer()
                        await self.update_panel()
                        return
                    self._no_approval_since = None
                    first = flow_approvals[0]
                    if isinstance(first, dict):
                        self._waiting_approval_id = str(first.get("id", "") or "") or None
                    else:
                        self._waiting_approval_id = str(getattr(first, "id", "") or "") or None
                    self.state.current_status = "waiting_approval"
                    self.state.last_event = f"решение для {flow_id[:12]}…"
                    self._waiting_flow_id = flow_id
                    # Interactive picker (master prompt §10): the user decides
                    # with keys (Y/N/D/Esc), never by typing an approval_id.
                    try:
                        action, _entry = await pick_approval(
                            self.gateway,
                            _PickerRendererAdapter(self),
                            flow_approvals,
                            key_bridge=self.key_bridge,
                        )
                    except Exception:
                        action, _entry = None, None
                    if action is PickerAction.APPROVE and self._waiting_approval_id:
                        await self._decide_pending_approval(True, "/approve")
                        return
                    if action is PickerAction.DENY and self._waiting_approval_id:
                        await self._decide_pending_approval(False, "/deny")
                        return
                    self.state.messages.append(render_approval_list(flow_approvals))
                    self._flush_renderer()
                    await self.update_panel()
                    return

                if outcome.status in (TerminalOutcomeStatus.ACCEPTED, TerminalOutcomeStatus.QUEUED):
                    self._no_approval_since = None
                    self.state.current_status = "running"
                    self._flush_renderer()
                    await asyncio.sleep(self.poll_interval_sec)
                    continue

                self._waiting_flow_id = None
                self._waiting_approval_id = None
                self._no_approval_since = None
                if outcome.status is TerminalOutcomeStatus.SUCCESS:
                    if waiter is not None:
                        # DONE: validate through the canonical waiter — it returns
                        # immediately because the flow is already terminal.
                        await self._await_canonical_terminal(waiter, flow_id)
                        return
                    # Non-canonical client: a bare get_flow DONE must NOT open the
                    # success gate (mirrors _poll_without_canonical_waiter).
                    outcome = TerminalOutcome(
                        status=TerminalOutcomeStatus.MALFORMED,
                        error_message=(
                            "Terminal success was not validated by the canonical "
                            "GatewayClient.wait_for_terminal(); refusing to present success."
                        ),
                    )

                self.state.terminal_outcome = outcome
                self.state.current_status = "idle"
                if outcome.is_success():
                    self.state.last_event = f"{flow_id[:12]}… DONE"
                else:
                    status = (
                        outcome.status.value
                        if isinstance(outcome.status, TerminalOutcomeStatus)
                        else str(outcome.status)
                    )
                    self.state.last_event = f"{flow_id[:12]}… {status}"
                self.state.messages.append(render_outcome(outcome))
                self._flush_renderer()
                await self.update_panel()
                return
        except asyncio.CancelledError:
            self._waiting_flow_id = None
            self._waiting_approval_id = None
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.CANCELLED,
                error_message="Local wait was cancelled.",
            )
            self.state.current_status = "cancelled"
            self.state.last_event = "отменено"
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
            await self.update_panel()
        except Exception:
            # Any escape from the wait (e.g. wait_for_terminal raising an
            # httpx/Gateway error) must render a redacted outcome and keep the
            # session alive — never propagate into run_interactive_loop.
            self._waiting_flow_id = None
            self._waiting_approval_id = None
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.ERROR,
                error_message="Gateway request failed; details withheld: [REDACTED]",
            )
            self.state.current_status = "idle"
            self.state.last_event = "ошибка gateway"
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
            await self.update_panel()

    async def _wait_runner(self, waiter: TerminalWaiterProtocol | None, flow_id: str) -> None:
        """Run one wait attempt, mapping every failure mode to a safe fixed outcome."""
        try:
            if waiter is not None:
                await self._await_canonical_terminal(waiter, flow_id)
            else:
                await self._poll_without_canonical_waiter(flow_id)
        except asyncio.CancelledError:
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.CANCELLED,
                error_message="Local wait was cancelled.",
            )
            self.state.current_status = "cancelled"
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
        except TimeoutError:
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.TIMEOUT,
                error_message="Local wait timed out.",
            )
            self.state.current_status = "timeout"
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()
        except Exception:
            self.state.terminal_outcome = TerminalOutcome(
                status=TerminalOutcomeStatus.ERROR,
                error_message="Gateway request failed.",
            )
            self.state.messages.append(render_outcome(self.state.terminal_outcome))
            self._flush_renderer()

    async def poll_active_flow(self, flow_id: str | None) -> None:
        """Poll flow status, preferring the canonical ``wait_for_terminal``.

        Uses ``CliRenderer.live_spinner()`` when ``self.enable_animations`` is True.
        """
        if not flow_id:
            return

        self._cancel_event.clear()

        waiter: TerminalWaiterProtocol | None = (
            self.gateway if isinstance(self.gateway, TerminalWaiterProtocol) else None
        )

        task = asyncio.create_task(self._wait_runner(waiter, flow_id))
        self._current_wait_task = task
        try:
            # Wrap in live_spinner when animations are enabled and renderer supports it
            if (
                self.enable_animations
                and self.renderer is not None
                and hasattr(self.renderer, "live_spinner")
            ):
                with self.renderer.live_spinner(
                    status_text=_phase_text(self.state.current_status)
                ):
                    await task
            else:
                await task
        finally:
            self._current_wait_task = None

    async def run_interactive_loop(
        self,
        session: PromptSession[str] | None = None,
        *,
        input_reader: Any | None = None,
    ) -> int:
        """Run interactive CLI prompt loop until exit/EOF or KeyboardInterrupt."""
        if self.renderer is None:
            self.renderer = CliRenderer()

        # Draw the static banner + status panel once, fed by live Gateway data.
        await self.render_initial_panel()

        # Start the calm background event monitor (drives panel + reconnect).
        await self.start_monitor()

        # Slash-меню: команды из Gateway /commands (реальные), локальные UI — отдельно.
        if session is None and input_reader is None:
            try:
                gateway_commands: list[dict[str, object]] | None = await self.gateway.list_commands()
            except Exception:
                gateway_commands = None
            catalog = merge_catalog(gateway_commands)
            session = create_prompt_session(
                catalog=catalog,
                bottom_toolbar=build_command_toolbar(
                    catalog, lambda: build_pipeline_bar(self.state)
                ),
            )
        prompt_line = HTML(
            '<style bg="#7C3AED" fg="#ffffff"> ⚡ antigona> </style>'
        )
        try:
            while True:
                if input_reader is not None:
                    raw_input = await input_reader()
                else:
                    raw_input = await read_prompt(prompt_str=prompt_line, session=session)
                if raw_input == "\x04":  # EOF
                    break
                if raw_input == "\x03":  # Ctrl+C
                    if self._current_wait_task is not None and not self._current_wait_task.done():
                        await self.cancel_local_wait()
                    else:
                        break
                    continue
                if raw_input == "\x0c":  # Ctrl+L (REDRAW)
                    self.renderer.render_state(self.state)
                    continue

                parsed = parse_command(raw_input)
                if parsed.kind == CommandKind.EXIT:
                    break

                await self.handle_input(raw_input)
            return 0
        finally:
            if self.renderer is not None:
                self.renderer.release()
            await self.close()

    async def close(self) -> None:
        """Close client and clean up resources."""
        await self.stop_monitor()
        if hasattr(self.gateway, "close") and callable(self.gateway.close):
            res = self.gateway.close()
            if asyncio.iscoroutine(res):
                await res