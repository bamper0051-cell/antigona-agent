"""Telegram transport layer — low-level Telegram API and Gateway integration.

Extracted from bot.py: handles Bot/Dispatcher lifecycle, Gateway HTTP/WS calls,
circuit breaker, dedup middleware, rate limiting, and approval idempotency.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import IO, Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    Message,
    Update,
)

from antigona.gateway.circuit_breaker import GatewayCircuitBreaker

try:
    import fcntl
except ImportError:  # Windows: no flock; pid-file locking degrades to best-effort
    fcntl = None  # type: ignore[assignment]

# ─── Constants ────────────────────────────────────────────────────────────────

PID_FILE = "/tmp/antigona_bot.pid"

TERMINAL_STATES = {
    "DONE",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "TIMEOUT",
    "POLICY_DENIED",
}

__all__ = [
    "TelegramTransport",
    "ApprovalCallback",
    "PidLockError",
    "acquire_pid_lock",
    "format_card",
    "make_approval_keyboard",
    "stream_flow_progress",
    "TERMINAL_STATES",
]


# ─── PID Lock ─────────────────────────────────────────────────────────────────


class PidLockError(RuntimeError):
    """The single-instance pid lock could not be *established* (fail closed).

    This is deliberately distinct from "another instance is already running".
    It signals an environment / configuration problem — a read-only code root
    (``EROFS``), permission denied (``EACCES`` / ``EPERM``), a missing or
    unwritable pid directory (``ENOENT`` / ``ENOTDIR``), a refused symlink
    (``ELOOP``), a full filesystem, and so on — and must be surfaced to the
    operator as an actionable error, never collapsed into a phantom 409 guard.
    """


def _pid_lock_hint(pid_file_path: str, reason: str) -> str:
    """Actionable, non-ambiguous message for a pid-lock setup failure."""
    return (
        f"cannot establish the Antigona bot single-instance lock at "
        f"{pid_file_path!r}: {reason}. The lock file must live on a writable, "
        f"governed runtime path (e.g. ANTIGONA_PID_FILE=/run/antigona/bot.pid "
        f"or /var/lib/antigona/bot.pid) and never under a read-only code root."
    )


def _is_lock_contention(exc: OSError) -> bool:
    """True only for a genuine concurrent holder (non-blocking lock refused)."""
    if isinstance(exc, BlockingIOError):
        return True
    return exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)


def acquire_pid_lock(pid_file_path: str | None = None) -> IO[str] | None:
    """Acquire an exclusive file lock so exactly one bot instance can run.

    Lock path resolution: an explicit ``pid_file_path`` argument, otherwise
    :func:`antigona.core.paths.pid_file` — which honours the
    ``ANTIGONA_PID_FILE`` environment variable and falls back to
    ``/tmp/antigona_bot.pid``.

    Return contract:

    * open file object — this process holds the lock;
    * ``None`` — a *genuine* concurrent holder already owns the lock
      (flock / msvcrt contention only);
    * raises :class:`PidLockError` — the lock could not be created at all
      (``EROFS`` read-only filesystem, ``EACCES`` / ``EPERM`` permission
      denied, ``ENOENT`` / ``ENOTDIR`` missing pid directory, ``ELOOP``
      symlink refused, ...). A setup failure is fail-closed and must never be
      reported as "another instance is already running".
    """
    from antigona.core import paths

    if pid_file_path is None:
        pid_file_path = str(paths.pid_file())

    fh: IO[str] | None = None
    try:
        _nf = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(pid_file_path, os.O_RDWR | os.O_CREAT | _nf, 0o600)
        fh = open(fd, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                fh.close()
                fh = None
                if _is_lock_contention(exc):
                    return None
                raise PidLockError(
                    _pid_lock_hint(pid_file_path, f"file lock refused: {exc}")
                ) from exc
        elif os.name == "nt":
            # BUG ANT-006: on Windows fcntl is absent and the previous fallback
            # skipped locking entirely — two bot instances could both acquire
            # the pid file. Use msvcrt byte-range locking (LK_NBLCK = non-
            # blocking) as the flock analogue.
            import msvcrt
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write("\0")
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                fh = None
                return None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        return fh
    except PidLockError:
        raise
    except OSError as exc:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        code = errno.errorcode.get(exc.errno) if exc.errno is not None else None
        detail = f"{exc.__class__.__name__} errno={exc.errno}"
        if code:
            detail += f" ({code})"
        if exc.strerror:
            detail += f": {exc.strerror}"
        raise PidLockError(_pid_lock_hint(pid_file_path, detail)) from exc


# ─── Approval Callback Data ────────────────────────────────────────────────────


class ApprovalCallback(CallbackData, prefix="appr"):
    approval_id: str
    action: str  # "approve" or "reject"
    flow_id: str = ""


# ─── Compact approval-token registry ──────────────────────────────────────────
# Telegram caps inline-button callback_data at 64 bytes; a full approval UUID
# (36 chars) plus a flow UUID (36 chars) overflows that. Buttons therefore pack
# only a compact token + action, and the real (approval_id, flow_id) are looked
# up here at callback time. The token is a deterministic 16-hex digest of the
# approval id, so re-rendering the same approval reuses one bounded entry.

_APPROVAL_TOKEN_REGISTRY: dict[str, tuple[str, str]] = {}
_APPROVAL_TOKEN_LEN = 16
_APPROVAL_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")


def _approval_token(approval_id: str) -> str:
    """Deterministic short token for an approval id (bounded, idempotent)."""
    return hashlib.sha256(approval_id.encode("utf-8")).hexdigest()[:_APPROVAL_TOKEN_LEN]


def register_approval_token(approval_id: str, flow_id: str) -> str:
    """Map a compact token to ``(approval_id, flow_id)`` and return the token."""
    token = _approval_token(approval_id)
    _APPROVAL_TOKEN_REGISTRY[token] = (approval_id, flow_id)
    return token


def resolve_approval_token(token: str) -> tuple[str, str] | None:
    """Resolve a callback token back to ``(approval_id, flow_id)`` or ``None``."""
    return _APPROVAL_TOKEN_REGISTRY.get(token)


def is_approval_token(value: str) -> bool:
    """Return ``True`` when ``value`` matches the compact approval-token format."""
    return bool(_APPROVAL_TOKEN_RE.match(value))


# ─── Card Formatting ──────────────────────────────────────────────────────────


def format_card(flow_data: dict[str, Any]) -> str:
    """Format a task flow data dict into a readable card text."""
    flow_id = flow_data.get("id", "N/A")
    goal = flow_data.get("goal", "N/A")
    status = flow_data.get("status", "UNKNOWN")
    target_path = flow_data.get("target_path", "")

    status_emoji = {
        "CREATED": "⏳",
        "RUNNING": "⚙️",
        "WAITING_APPROVAL": "⚠️",
        "VERIFYING": "🔍",
        "DONE": "✅",
        "FAILED": "❌",
        "CANCELLED": "🚫",
        "TIMEOUT": "⏰",
        "POLICY_DENIED": "🛑",
    }.get(status, "ℹ️")

    lines = [
        "📋 Antigona Task Flow",
        f"• ID: {flow_id}",
        f"• Goal: {goal}",
    ]
    if target_path:
        lines.append(f"• Target: {target_path}")
    lines.append(f"• Status: {status} {status_emoji}")

    steps = flow_data.get("steps", [])
    if steps:
        lines.append("\nSteps:")
        for s in steps:
            st_name = s.get("title", f"Step {s.get('index', 0)}")
            st_status = s.get("status", "")
            lines.append(f"- {st_name}: {st_status}")

    approvals = flow_data.get("approvals", [])
    pending = [a for a in approvals if a.get("decision") == "PENDING"]
    if pending:
        lines.append("\n⚠️ Pending Approvals:")
        for p in pending:
            risk = p.get("risk_level", "HIGH")
            reason = p.get("reason", "Security check required")
            lines.append(f"- [{risk}] {reason}")

    return "\n".join(lines)


def make_approval_keyboard(flow_data: dict[str, Any]) -> Any:
    """Create an inline keyboard with approve/reject buttons for pending approvals."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    approvals = flow_data.get("approvals", [])
    pending = [a for a in approvals if a.get("decision") == "PENDING"]
    if not pending:
        return None

    buttons: list[list[InlineKeyboardButton]] = []
    for app in pending:
        app_id = str(app.get("id", ""))
        flow_id = str(flow_data.get("id", ""))
        # Pack only a compact token + action to stay under Telegram's 64-byte
        # callback_data limit; the real ids are resolved via the registry.
        token = register_approval_token(app_id, flow_id)
        btn_approve = InlineKeyboardButton(
            text="✅ Approve",
            callback_data=ApprovalCallback(approval_id=token, action="approve").pack(),
        )
        btn_reject = InlineKeyboardButton(
            text="❌ Reject",
            callback_data=ApprovalCallback(approval_id=token, action="reject").pack(),
        )
        buttons.append([btn_approve, btn_reject])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── WebSocket Progress Stream ─────────────────────────────────────────────────


async def stream_flow_progress(
    bot: Bot,
    chat_id: int,
    message_id: int,
    flow_id: str,
    gateway_url: str,
    gateway_token: str,
    get_flow_fn: Callable[[str], Any],
) -> None:
    """Stream flow progress updates via WebSocket and update the card message."""
    import websockets

    ws_base = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_base}/flows/{flow_id}/progress?token={gateway_token}"

    last_edit_time = 0.0
    throttle_interval = 0.5
    last_text = ""

    try:
        async with websockets.connect(ws_url) as ws:
            async for raw_msg in ws:
                data = json.loads(raw_msg)
                msg_type = data.get("type")

                try:
                    flow_data = await get_flow_fn(flow_id)
                except Exception:
                    flow_data = {"id": flow_id, "status": data.get("to_state", "RUNNING")}

                card_text = format_card(flow_data)
                reply_markup = make_approval_keyboard(flow_data)

                # Send long log output as file attachment
                steps = flow_data.get("steps", [])
                for step in steps:
                    output = step.get("output") or {}
                    if isinstance(output, dict):
                        stdout = output.get("stdout", "") or output.get("text", "")
                        if isinstance(stdout, str) and len(stdout) > 1000:
                            doc = BufferedInputFile(
                                stdout.encode("utf-8"),
                                filename=f"flow_{flow_id}_step_{step.get('id', '1')}.log",
                            )
                            try:
                                await bot.send_document(
                                    chat_id=chat_id,
                                    document=doc,
                                    caption=f"Log file for flow {flow_id}",
                                )
                            except Exception:
                                pass

                now = time.time()
                if card_text != last_text and (now - last_edit_time >= throttle_interval):
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=card_text,
                            reply_markup=reply_markup,
                        )
                        last_text = card_text
                        last_edit_time = now
                    except Exception:
                        pass

                if msg_type == "end" or flow_data.get("status") in TERMINAL_STATES:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=format_card(flow_data),
                            reply_markup=make_approval_keyboard(flow_data),
                        )
                    except Exception:
                        pass
                    break
    except Exception as exc:
        logging.warning("WebSocket stream exception for flow %s: %s", flow_id, exc)


# ─── Telegram Transport ────────────────────────────────────────────────────────


class TelegramTransport:
    """Low-level Telegram transport. Owns Bot, Dispatcher, and Gateway integration.

    Responsibilities:
      - Bot/Dispatcher lifecycle
      - Gateway HTTP calls (post_flow, get_flow, post_approval_decision)
      - Circuit breaker and recovery
      - Update-ID deduplication middleware
      - Per-chat rate limiting
      - Approval idempotency
      - WebSocket progress streaming
    """

    def __init__(
        self,
        token: str | None = None,
        gateway_url: str = "http://127.0.0.1:8090",
        gateway_token: str = "gateway-token",
        storage: MemoryStorage | None = None,
    ) -> None:
        tok = token or os.getenv("TELEGRAM_BOT_TOKEN") or "dummy-bot-token"
        self.token: str = tok
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_token = gateway_token
        self.storage = storage or MemoryStorage()
        self.bot = Bot(token=self.token)
        self.dp = Dispatcher(storage=self.storage)
        self.circuit_breaker = GatewayCircuitBreaker()
        # ── Day 7: Update-ID deduplication ──
        self._processed_updates: set[int] = set()
        self._max_processed_updates: int = 200
        # ── Day 7: Approval idempotency ──
        self._processed_approvals: set[str] = set()
        # ── Day 7: Per-chat rate limiting ──
        self._last_response_time: dict[int, float] = {}
        self._rate_limit_sec: float = 0.5
        # ── Day 7: Dedup middleware ──
        self._register_dedup_middleware()
        # Background recovery probe loop (lazy: created on first poll)
        self._recovery_task: asyncio.Task[None] | None = None

    def _ensure_recovery_task(self) -> None:
        """Start the background recovery loop if not already running."""
        if self._recovery_task is None:
            try:
                loop = asyncio.get_running_loop()
                self._recovery_task = loop.create_task(self._recovery_loop())
            except RuntimeError:
                pass  # No running loop (e.g. in tests)

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.gateway_token}"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    # ── Gateway HTTP calls ─────────────────────────────────────────────────

    async def post_flow(
        self,
        goal: str,
        path: str = "task_output.txt",
        content: str = "",
        tool_name: str = "workspace.write_text",
        command: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a flow on the Gateway. Records success/failure on the circuit breaker."""
        idempotency_key = f"tg-flow-{int(time.time()*1000)}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.gateway_url}/flows",
                    json={
                        "goal": goal,
                        "path": path,
                        "content": content,
                        "tool_name": tool_name,
                        "command": command or [],
                    },
                    headers=self._headers(idempotency_key),
                )
                resp.raise_for_status()
                res: dict[str, Any] = resp.json()
                self.circuit_breaker.record_success()
                return res
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
            # Connection-level errors: gateway is likely down
            self.circuit_breaker.record_failure()
            raise

    async def get_flow(self, flow_id: str) -> dict[str, Any]:
        """Fetch a flow's current state from the Gateway."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.gateway_url}/flows/{flow_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            res: dict[str, Any] = resp.json()
            return res

    async def post_approval_decision(self, approval_id: str, approve: bool) -> dict[str, Any]:
        """Submit an approval decision to the Gateway."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/approvals/{approval_id}/decision",
                json={"approve": approve},
                headers=self._headers(),
            )
            resp.raise_for_status()
            res: dict[str, Any] = resp.json()
            return res

    # ── Circuit breaker recovery ──────────────────────────────────────────

    async def _recovery_loop(self) -> None:
        """Background task that probes Gateway when circuit is OPEN."""
        while True:
            await asyncio.sleep(5)
            if self.circuit_breaker.state != "OPEN":
                continue
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{self.gateway_url}/health",
                        headers=self._headers(),
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        self.circuit_breaker.record_success()
                        await self._flush_deferred_queue()
            except Exception:
                pass  # Gateway still down, keep waiting

    async def _flush_deferred_queue(self) -> None:
        """Send all deferred tasks to the Gateway now that it's back up."""
        tasks = self.circuit_breaker.flush_tasks()
        if not tasks:
            return

        logging.info(
            "Gateway recovered, flushing %d deferred tasks",
            len(tasks),
        )

        for task in tasks:
            try:
                await self.post_flow(
                    goal=task.goal,
                    path=task.path,
                    content=task.content,
                    tool_name=task.tool_name,
                    command=task.command,
                )
            except Exception:
                logging.warning("Failed to send deferred task: %s", task.goal)
                # Re-enqueue on failure
                self.circuit_breaker.enqueue_task(task)

    # ── Day 7: Update-ID dedup middleware ────────────────────────────

    def _register_dedup_middleware(self) -> None:
        """Register an update processing middleware that skips duplicate update_ids."""

        @self.dp.update.outer_middleware()  # type: ignore[arg-type,call-arg,untyped-decorator]
        async def dedup_middleware(
            handler: Callable[..., Any],
            event: Update,
            data: dict[str, Any],
        ) -> Any:
            uid = event.update_id
            if uid in self._processed_updates:
                return None  # skip entirely
            self._processed_updates.add(uid)
            # Enforce max size
            if len(self._processed_updates) > self._max_processed_updates:
                # Keep only the most recent half
                cutoff = self._max_processed_updates // 2
                self._processed_updates = set(list(self._processed_updates)[-cutoff:])
            return await handler(event, data)

    # ── Day 7: Idempotency helpers ─────────────────────────────────────

    def _approval_key(self, approval_id: str, action: str) -> str:
        return f"{approval_id}:{action}"

    def _is_approval_processed(self, approval_id: str, action: str) -> bool:
        return self._approval_key(approval_id, action) in self._processed_approvals

    def _mark_approval_processed(self, approval_id: str, action: str) -> None:
        self._processed_approvals.add(self._approval_key(approval_id, action))
        # Prevent unbounded growth: keep last 200 keys
        if len(self._processed_approvals) > 200:
            self._processed_approvals = set(list(self._processed_approvals)[-100:])

    # ── Day 7: Rate limiting ───────────────────────────────────────────

    async def _enforce_rate_limit(self, chat_id: int) -> None:
        """Wait if the last response to this chat_id was less than RATE_LIMIT_SEC ago."""
        now = time.monotonic()
        last = self._last_response_time.get(chat_id, 0.0)
        elapsed = now - last
        if elapsed < self._rate_limit_sec:
            await asyncio.sleep(self._rate_limit_sec - elapsed)

    def _update_last_response_time(self, chat_id: int) -> None:
        self._last_response_time[chat_id] = time.monotonic()
        # Prevent unbounded growth
        if len(self._last_response_time) > 1000:
            self._last_response_time.clear()

    # ── Day 7: Approval state reducer ──────────────────────────────────

    async def _update_approval_card(
        self,
        query: Any,
        approval_id: str,
        action: str,
        decision: str,
    ) -> None:
        """Update the task flow card after an approval decision."""
        if not query.message or not isinstance(query.message, Message):
            return
        try:
            # Try to find the flow ID from the card text
            curr_text = query.message.text or ""
            flow_id = ""
            for line in curr_text.split("\n"):
                if line.startswith("• ID:"):
                    flow_id = line.split(":", 1)[-1].strip()
                    break

            status_line = (
                "✅ Approved" if action == "approve"
                else "❌ Rejected"
            )

            if flow_id:
                try:
                    flow_data = await self.get_flow(flow_id)
                    new_card = format_card(flow_data)
                    await query.message.edit_text(
                        text=new_card + f"\n\n👉 {status_line}",
                        reply_markup=None,
                    )
                    return
                except Exception:
                    pass  # Fall through to simple edit

            # Simple edit if flow data unavailable
            await query.message.edit_text(
                text=curr_text + f"\n\n👉 {status_line}",
                reply_markup=None,
            )
        except Exception:
            pass

    # ── Send methods ───────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> Message | None:
        """Send a plain text message via the bot."""
        try:
            return await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as exc:
            logging.warning("Failed to send message to %d: %s", chat_id, exc)
            return None

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any = None,
    ) -> None:
        """Edit a message."""
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except Exception:
            pass

    async def send_document(
        self,
        chat_id: int,
        document: BufferedInputFile,
        caption: str = "",
    ) -> None:
        """Send a document/file to a chat."""
        try:
            await self.bot.send_document(
                chat_id=chat_id,
                document=document,
                caption=caption,
            )
        except Exception:
            pass
