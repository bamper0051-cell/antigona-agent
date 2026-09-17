"""Tmux session tool — quiet, owner-gated background process management.

"Shy" by design: a command starts in a detached tmux session and the tool
returns only a compact JSON summary (session name + a short output tail).
Nothing is streamed into the chat while the command runs; the owner can
``read``, ``send``, ``list`` or ``kill`` sessions on demand.

Owner gate (hard deny): the tool refuses to run unless the authenticated
owner of the turn (threaded by ``DialogueEngine`` as the reserved
``_owner_id`` kwarg) matches ``ANTIGONA_OWNER_ID``.  There is no approval
path — a non-owner gets a refusal without the ability to request access.
The gate is fail-closed: if ``ANTIGONA_OWNER_ID`` is not configured or the
owner context is missing, the tool denies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from antigona.security.owner_identity import OwnerIdentity

logger = logging.getLogger(__name__)

#: tmux session names cannot contain ':', '/' or start with '.' — we keep a
#: conservative charset and cap the length.
_SESSION_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_SESSION_LEN = 40
_CMD_TIMEOUT = 10.0
_MAX_OUTPUT = 4000
_DEFAULT_LINES = 40

#: Same destructive-pattern guard as run_shell (never touch system state).
_BLOCKED_SUBSTRINGS = (
    "shutdown",
    "reboot",
    "mkfs",
    "rm -rf /",
    "passwd",
    "iptables -F",
    "dd if=/dev/zero",
)

_ACTIONS = ("start", "send", "read", "list", "kill", "status")


def _sanitize_session(name: str) -> str:
    """Return a tmux-safe session name (charset + length capped)."""
    cleaned = _SESSION_RE.sub("-", (name or "").strip())
    return cleaned[:_MAX_SESSION_LEN] or f"antigona-{int(time.time())}"


def _owner_gate(owner_id: str) -> str | None:
    """Hard owner gate: error string when the caller is not the owner."""
    identity = OwnerIdentity()
    if not identity.is_configured:
        return "tmux: владелец не сконфигурирован (ANTIGONA_OWNER_ID) — доступ закрыт"
    try:
        uid = int(owner_id)
    except (TypeError, ValueError):
        return "tmux: контекст владельца отсутствует — доступ только владельцу"
    if not identity.is_owner(uid):
        return f"tmux: доступ только владельцу (ANTIGONA_OWNER_ID={identity.owner_user_id})"
    return None


def _blocked(command: str) -> str | None:
    low = command.lower()
    for pattern in _BLOCKED_SUBSTRINGS:
        if pattern in low:
            return f"tmux: команда заблокирована: {command}"
    return None


def _workspace_dir() -> str:
    """Default working directory for new sessions — the governed workspace root."""
    from antigona.core import paths

    ws = paths.workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    return str(ws)


def _confine_cwd(cwd_raw: str) -> tuple[str, str | None]:
    """Resolve *cwd_raw* strictly inside the configured workspace.

    Returns ``(resolved_cwd, None)`` on success, or ``("", error)`` when the
    path traverses out of, resolves outside, or does not name an existing
    directory within the configured workspace root.  ``start`` must never hand
    an out-of-workspace working directory to the host tmux process.
    """
    from antigona.security.risk_classifier import resolve_workspace_root

    ws_root = resolve_workspace_root()
    if ws_root is None:
        return "", "tmux: workspace не сконфигурирован — доступ закрыт"

    escape = "tmux: cwd вне рабочего пространства (workspace) — доступ закрыт"
    raw = Path(cwd_raw.strip()) if cwd_raw.strip() else ws_root
    if any(part == ".." for part in raw.parts):
        return "", escape
    try:
        resolved = raw.resolve() if raw.is_absolute() else (ws_root / raw).resolve()
    except (OSError, ValueError, RuntimeError):
        return "", escape
    if resolved != ws_root and ws_root not in resolved.parents:
        return "", escape
    if not resolved.is_dir():
        return "", "tmux: cwd не существует или не является директорией"
    return str(resolved), None


async def _run_tmux(args: list[str], timeout: float = _CMD_TIMEOUT) -> tuple[int, str, str]:
    """Run tmux with an argument list (no shell), bounded by a timeout."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "timeout"
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _handle_tmux(**kwargs: Any) -> str:
    """Owner-gated tmux session management; returns a compact JSON string."""
    owner_id = str(kwargs.pop("_owner_id", "") or "")
    gate = _owner_gate(owner_id)
    if gate:
        return json.dumps({"error": gate})

    if shutil.which("tmux") is None:
        return json.dumps({"error": "tmux не установлен"})

    action = str(kwargs.get("action", "") or "list").lower()
    if action not in _ACTIONS:
        return json.dumps(
            {"error": f"unknown action '{action}'; expected: {', '.join(_ACTIONS)}"}
        )

    session = _sanitize_session(str(kwargs.get("session", "") or f"antigona-{int(time.time())}"))
    command = str(kwargs.get("command", "") or "")
    keys = str(kwargs.get("keys", "") or "")
    try:
        lines = max(1, min(int(kwargs.get("lines", _DEFAULT_LINES) or _DEFAULT_LINES), 200))
    except (TypeError, ValueError):
        lines = _DEFAULT_LINES
    cwd = str(kwargs.get("cwd", "") or _workspace_dir())

    for payload in (command, keys):
        if payload and _blocked(payload):
            return json.dumps({"error": _blocked(payload)})

    try:
        if action == "start":
            if not command:
                return json.dumps(
                    {"error": "start требует command (запуск в отсоединённой сессии)"}
                )
            safe_cwd, cwd_err = _confine_cwd(cwd)
            if cwd_err:
                return json.dumps({"error": cwd_err})
            # Idempotently ensure the tmux server socket exists before creating a
            # session. In headless CI runners the server is not up until the first
            # command touches it; without this, `new-session -d` fails with
            # "error connecting to /tmp/tmux-<uid>/default (No such file or
            # directory)" on the very first start.
            await _run_tmux(["start-server"])
            rc, _out, err = await _run_tmux(
                ["new-session", "-d", "-s", session, "-c", safe_cwd, command]
            )
            if rc != 0:
                return json.dumps({"error": err.strip() or f"tmux exit {rc}", "session": session})
            # Quiet: return only a short tail, never stream the whole command output.
            rc2, out2, _err2 = await _run_tmux(
                ["capture-pane", "-pt", session, "-S", "-5"]
            )
            tail = out2.strip()[-_MAX_OUTPUT:] if rc2 == 0 else ""
            return json.dumps({"success": True, "session": session, "action": "start", "tail": tail})

        if action == "send":
            if not keys:
                return json.dumps({"error": "send требует keys"})
            rc, _out, err = await _run_tmux(["send-keys", "-t", session, keys, "Enter"])
            if rc != 0:
                return json.dumps({"error": err.strip() or f"tmux exit {rc}", "session": session})
            return json.dumps({"success": True, "session": session, "sent": keys[:80]})

        if action == "read":
            rc, out, err = await _run_tmux(
                ["capture-pane", "-pt", session, "-S", f"-{lines}"]
            )
            if rc != 0:
                return json.dumps({"error": err.strip() or f"tmux exit {rc}", "session": session})
            return json.dumps(
                {"success": True, "session": session, "lines": out.strip()[-_MAX_OUTPUT:]}
            )

        if action == "list":
            rc, out, err = await _run_tmux(["list-sessions", "-F", "#{session_name}"])
            if rc != 0:
                return json.dumps({"error": err.strip() or f"tmux exit {rc}"})
            names = [line for line in out.splitlines() if line.strip()]
            return json.dumps({"success": True, "sessions": names})

        if action == "status":
            rc, _out, _err = await _run_tmux(["has-session", "-t", session])
            return json.dumps({"success": True, "session": session, "running": rc == 0})

        if action == "kill":
            rc, _out, err = await _run_tmux(["kill-session", "-t", session])
            if rc != 0:
                return json.dumps({"error": err.strip() or f"tmux exit {rc}", "session": session})
            return json.dumps({"success": True, "session": session, "killed": True})
    except Exception as exc:  # noqa: BLE001
        logger.exception("tmux tool failed")
        return json.dumps({"error": str(exc)})

    return json.dumps({"error": "unreachable"})


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": "start|send|read|list|kill|status",
        },
        "session": {"type": "string", "description": "tmux session name (sanitized)"},
        "command": {"type": "string", "description": "command to run in a new detached session"},
        "keys": {"type": "string", "description": "keys to send to a session (Enter appended)"},
        "lines": {"type": "integer", "description": "lines to capture (read), default 40"},
        "cwd": {"type": "string", "description": "working directory for start (default workspace)"},
    },
    "required": ["action"],
}


def register(registry: Any) -> None:
    """Register the tmux tool on the given ``ToolRegistry``."""
    registry.register(
        "tmux",
        toolset="shell",
        schema=_SCHEMA,
        handler=_handle_tmux,
    )


__all__ = ["_handle_tmux", "register"]
