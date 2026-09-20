"""ActionExecutor — parse and execute LLM action commands.

Extracts structured commands (WRITE_FILE, SEND_FILE, RUN_SHELL, CONFIGURE_KEY, etc.)
from LLM responses and executes them directly with PolicyEngine and SystemAuditLogger integration.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.ownership.epoch import FenceDeniedError
from antigona.ownership.wiring import enforce_write_fence
from antigona.policy.engine import PolicyEngine
from antigona.security.audit import SystemAuditLogger
from antigona.security.owner_override import OwnerOverrideManager

logger = logging.getLogger(__name__)

# ─── Action types ─────────────────────────────────────────────────────────────


class ActionType(StrEnum):
    WRITE_FILE = "WRITE_FILE"
    SEND_FILE = "SEND_FILE"
    RUN_SHELL = "RUN_SHELL"
    CONFIGURE_KEY = "CONFIGURE_KEY"
    GENERATE_IMAGE = "GENERATE_IMAGE"
    MEMORIZE = "MEMORIZE"
    WEB_SEARCH = "WEB_SEARCH"
    READ_FILE = "READ_FILE"
    SEARCH_FILES = "SEARCH_FILES"
    RUN_CODE = "RUN_CODE"


_WRITE_RE = re.compile(
    r"WRITE_FILE\|(.+?)\|(.*)",
    re.MULTILINE,
)
_SEND_RE = re.compile(
    r"SEND_FILE\|(.+)", re.IGNORECASE
)
_SHELL_RE = re.compile(
    r"RUN_SHELL\|(.+)", re.IGNORECASE
)
_CONFIGURE_KEY_RE = re.compile(
    r"CONFIGURE_KEY\|(.+?)\|(.+)", re.IGNORECASE
)
_IMAGE_RE = re.compile(
    r"GENERATE_IMAGE\|(.+)", re.IGNORECASE
)
_MEMORIZE_RE = re.compile(
    r"MEMORIZE\|(memory|user)\|(.+?)(?=\n(?:WRITE_FILE|SEND_FILE|RUN_SHELL|CONFIGURE_KEY|GENERATE_IMAGE|MEMORIZE|STEP)|\Z)",
    re.DOTALL | re.MULTILINE,
)
_READ_FILE_RE = re.compile(
    r"READ_FILE\|(.+)", re.IGNORECASE
)
_SEARCH_FILES_RE = re.compile(
    r"SEARCH_FILES\|(.+?)\|(.+)", re.IGNORECASE
)
_RUN_CODE_RE = re.compile(
    r"RUN_CODE\|(.+?)\|(.+?)(?=\n(?:WRITE_FILE|SEND_FILE|RUN_SHELL|CONFIGURE_KEY|GENERATE_IMAGE|MEMORIZE|READ_FILE|SEARCH_FILES|RUN_CODE|STEP)|\Z)",
    re.DOTALL | re.MULTILINE,
)


class ExecutionMode(Enum):
    DIRECT = "direct"
    CONFIRM = "confirm"


class Action:
    type: ActionType
    path: str = ""
    content: str = ""
    command: str = ""
    raw: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        type: ActionType | None = None,
        path: str = "",
        content: str = "",
        command: str = "",
        raw: str = "",
        metadata: dict[str, Any] | None = None,
        action_type: ActionType | None = None,
        provider: str | None = None,
        key: str | None = None,
    ) -> None:
        actual_type = type if type is not None else action_type
        if actual_type is None:
            raise TypeError("Action requires 'type' or 'action_type'")
        self.type = actual_type
        self.path = path if provider is None else provider
        self.content = content if key is None else key
        self.command = command
        self.raw = raw
        self.metadata = metadata if metadata is not None else {}

    @property
    def action_type(self) -> ActionType:
        return self.type

    @action_type.setter
    def action_type(self, val: ActionType) -> None:
        self.type = val

    @property
    def provider(self) -> str:
        return self.path

    @provider.setter
    def provider(self, val: str) -> None:
        self.path = val

    @property
    def key(self) -> str:
        return self.content

    @key.setter
    def key(self, val: str) -> None:
        self.content = val


@dataclass
class ActionResult:
    success: bool
    action_type: ActionType
    message: str = ""
    path: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ActionExecutor:
    def __init__(
        self,
        mode: ExecutionMode = ExecutionMode.DIRECT,
        owner_override: OwnerOverrideManager | None = None,
        policy_engine: PolicyEngine | None = None,
        audit_logger: SystemAuditLogger | None = None,
        delivery_adapter_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.mode = mode
        self.owner_override = owner_override
        self.policy_engine = policy_engine or PolicyEngine(owner_override=owner_override)
        self.audit_logger = audit_logger or SystemAuditLogger()
        self.delivery_adapter_factory = delivery_adapter_factory
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_sessions: set[str] = set()

    def _get_delivery_adapter(self) -> Any:
        """Построить delivery-адаптер лениво.

        По умолчанию собирает реальный Telegram delivery adapter так же, как
        ``antigona.delivery.factory``. Если задан ``delivery_adapter_factory`` —
        используется он (например, для тестов).
        """
        if self.delivery_adapter_factory is not None:
            return self.delivery_adapter_factory()
        from antigona.config import Settings
        from antigona.delivery.factory import get_adapter

        return get_adapter("telegram", Settings.from_env())

    def cancel_session(self, session_id: str) -> bool:
        """Отменить выполняемую команду в указанной сессии (/cancel)."""
        sid = str(session_id)
        self._cancelled_sessions.add(sid)
        proc = self._running_processes.get(sid)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            logger.info("ActionExecutor: Завершён процесс команды в сессии %s", sid)
            return True
        return True

    def parse_action_from_llm(self, text: str) -> list[Action]:
        if not text:
            return []
        actions: list[Action] = []
        for m in _WRITE_RE.finditer(text):
            actions.append(Action(
                type=ActionType.WRITE_FILE,
                path=m.group(1).strip(),
                content=m.group(2).strip(),
                raw=m.group(0),
            ))
        for m in _SEND_RE.finditer(text):
            actions.append(Action(
                type=ActionType.SEND_FILE,
                path=m.group(1).strip(),
                raw=m.group(0),
            ))
        for m in _SHELL_RE.finditer(text):
            actions.append(Action(
                type=ActionType.RUN_SHELL,
                command=m.group(1).strip(),
                raw=m.group(0),
            ))
        for m in _CONFIGURE_KEY_RE.finditer(text):
            actions.append(Action(
                type=ActionType.CONFIGURE_KEY,
                path=m.group(1).strip(),
                content=m.group(2).strip(),
                raw=m.group(0),
            ))
        for m in _IMAGE_RE.finditer(text):
            actions.append(Action(
                type=ActionType.GENERATE_IMAGE,
                content=m.group(1).strip(),
                raw=m.group(0),
            ))
        for m in _MEMORIZE_RE.finditer(text):
            store = m.group(1).strip()
            content = m.group(2).strip()
            title = content[:50].strip()
            actions.append(Action(
                type=ActionType.MEMORIZE,
                path=store,
                content=content,
                raw=m.group(0),
                metadata={"store": store, "title": title},
            ))
        for m in _READ_FILE_RE.finditer(text):
            actions.append(Action(
                type=ActionType.READ_FILE,
                path=m.group(1).strip(),
                raw=m.group(0),
            ))
        for m in _SEARCH_FILES_RE.finditer(text):
            actions.append(Action(
                type=ActionType.SEARCH_FILES,
                path=m.group(2).strip(),
                content=m.group(1).strip(),
                raw=m.group(0),
            ))
        for m in _RUN_CODE_RE.finditer(text):
            actions.append(Action(
                type=ActionType.RUN_CODE,
                path=m.group(1).strip(),  # language
                content=m.group(2).strip(),  # code
                raw=m.group(0),
            ))
        return actions

    def strip_action_commands(self, text: str) -> str:
        if not text:
            return ""
        lines = text.split("\n")
        cleaned: list[str] = []
        for line in lines:
            stripped = line.strip()
            if re.match(r"^(WRITE_FILE|SEND_FILE|RUN_SHELL|CONFIGURE_KEY|GENERATE_IMAGE|MEMORIZE|READ_FILE|SEARCH_FILES|RUN_CODE)\|", stripped):
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()

    def execute(self, action: Action, **kwargs: Any) -> Any:
        """Execute a single action (supports both sync and async callers)."""
        from antigona.observability_legacy import record as _legacy_record
        _legacy_record("action_executor.execute")
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                return self._async_execute(action, **kwargs)
        except RuntimeError:
            pass
        return asyncio.run(self._async_execute(action, **kwargs))

    def _actor_is_owner(self, channel: str, user_id: str, session_id: str) -> bool:
        """Is the calling actor an authenticated owner? Fail-closed.

        Three proofs are accepted, in order of cost:

        1. ``user_id`` is the configured Telegram owner id;
        2. the ``(channel, user_id, session_id)`` triple has a live
           PIN-elevated OwnerOverride session;
        3. the surface is not Telegram and the process runs as the configured
           CLI owner (``ANTIGONA_CLI_OWNER_USER`` / owner token).

        A surface that supplies no identity matches none of them and is denied.
        """
        from antigona.security.owner_identity import OwnerIdentity

        if user_id.lstrip("-").isdigit() and OwnerIdentity().is_owner(int(user_id)):
            return True

        manager = self.owner_override
        if manager is None:
            return False

        try:
            if manager.is_elevated(channel, user_id, session_id):
                return True
        except Exception:
            logger.debug("owner_override.is_elevated failed", exc_info=True)

        if channel != "telegram":
            try:
                return bool(manager.is_cli_owner())
            except Exception:
                logger.debug("owner_override.is_cli_owner failed", exc_info=True)
        return False

    async def _async_execute(self, action: Action, **kwargs: Any) -> ActionResult:
        handlers = {
            ActionType.WRITE_FILE: self._execute_write_file,
            ActionType.SEND_FILE: self._execute_send_file,
            ActionType.RUN_SHELL: self._execute_run_shell,
            ActionType.CONFIGURE_KEY: self._execute_configure_key,
            ActionType.GENERATE_IMAGE: self._execute_generate_image,
            ActionType.MEMORIZE: self._execute_memorize,
            ActionType.READ_FILE: self._execute_read_file,
            ActionType.SEARCH_FILES: self._execute_search_files,
            ActionType.RUN_CODE: self._execute_run_code,
        }
        handler = handlers.get(action.type)
        if handler is None:
            raise ValueError(f"Unknown action type: {action.type}")

        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))

        message = kwargs.get("message")
        # Identity used by the auth gate below. Telegram fills it from the
        # update; every other surface must pass ``user_id``/``session_id``
        # explicitly through kwargs. Anything left at its placeholder default
        # is treated as "no identity" and denied — see the gate below.
        pin_session_id: int | None = None
        if message is not None:
            chat_id = getattr(message, "chat", None)
            chat_id = getattr(chat_id, "id", None) if chat_id else chat_id
            user_obj = getattr(message, "from_user", None)
            if user_obj and getattr(user_obj, "id", None):
                user_id = str(user_obj.id)
            if chat_id:
                channel = "telegram"
                session_id = str(chat_id)
                pin_session_id = int(chat_id)
        elif session_id.lstrip("-").isdigit():
            pin_session_id = int(session_id)

        kwargs["channel"] = channel
        kwargs["user_id"] = user_id
        kwargs["session_id"] = session_id

        cmd_or_content = action.command or action.content or ""

        # ── POLICY CHECK & OWNER OVERRIDE ────────────────────────────────────
        verdict = await self.policy_engine.check(
            action.type.value if hasattr(action.type, "value") else str(action.type),
            params={
                "command": action.command,
                "content": action.content,
                "path": action.path,
            },
            context={
                "channel": channel,
                "user_id": user_id,
                "session_id": session_id,
            },
        )

        if verdict.get("requires_2step_confirmation"):
            pending = verdict.get("pending_confirmation")
            return ActionResult(
                success=False,
                action_type=action.type,
                message=verdict.get("formatted_message", "CRITICAL action requires confirmation."),
                error="2STEP_CONFIRMATION_REQUIRED",
                metadata={"pending_confirmation": pending},
            )

        if not verdict.get("allowed", False):
            self.audit_logger.log_action(
                channel=channel,
                user_id=user_id,
                session_id=session_id,
                command=cmd_or_content or action.path or str(action.type),
                exit_code=1,
                status="DENIED",
                details={"reason": verdict.get("reason")},
            )
            return ActionResult(
                success=False,
                action_type=action.type,
                message=verdict.get("reason", "Отклонено политикой безопасности."),
                error="POLICY_DENIAL",
            )

        # ── RISK CLASSIFICATION & PIN GATE (Telegram compatibility) ─────────
        from antigona.security.risk_classifier import RiskClassifier

        classifier = RiskClassifier()
        action_type_str = action.type.value if hasattr(action.type, "value") else str(action.type)
        risk_level = classifier.classify(
            action_type=action_type_str,
            path=action.path,
            content=action.content or action.command,
        )

        # The gate below is surface-independent on purpose. It used to live
        # inside ``if message is not None:``, so every non-Telegram caller (CLI,
        # agent loop, gateway, plugins) reached the handler with no
        # authorization at all. Now a surface that cannot supply an identity is
        # denied rather than trusted.
        if classifier.requires_auth(risk_level):
            if not self._actor_is_owner(channel, user_id, session_id):
                self.audit_logger.log_action(
                    channel=channel,
                    user_id=user_id,
                    session_id=session_id,
                    command=cmd_or_content or action.path or str(action.type),
                    exit_code=1,
                    status="DENIED",
                    details={"reason": "owner_gate", "risk": str(risk_level)},
                )
                return ActionResult(
                    success=False,
                    action_type=action.type,
                    message="🚫 Доступ запрещён. Только владелец может выполнять это действие.",
                    error="ACCESS_DENIED",
                )

        from antigona.tools.pin_gate import is_pin_configured, is_verified, requires_pin

        if requires_pin(action_type_str) and is_pin_configured():
            if pin_session_id is None or not is_verified(pin_session_id):
                return ActionResult(
                    success=False,
                    action_type=action.type,
                    message="🔒 Требуется PIN-код. Введите его командой:\n/pin <код>\n(действие заблокировано)",
                    error="PIN_REQUIRED",
                )

        return await handler(action, **kwargs)

    def execute_all(self, actions: list[Action], **kwargs: Any) -> Any:
        """Execute multiple actions (supports both sync and async callers)."""
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                return self._async_execute_all(actions, **kwargs)
        except RuntimeError:
            pass
        return asyncio.run(self._async_execute_all(actions, **kwargs))

    async def _async_execute_all(self, actions: list[Action], **kwargs: Any) -> list[ActionResult]:
        return [await self._async_execute(a, **kwargs) for a in actions]

    # ── Tool Gateway ─────────────────────────────────────────────────────────

    def _get_backend_for(self, tool_name: str) -> Any | None:
        from antigona.tools.image_gen import ImageGenerator

        implementations: dict[str, dict[str, Any]] = {
            "image_gen": {"pollinations": ImageGenerator},
        }
        defaults: dict[str, Any] = {"image_gen": ImageGenerator}
        fallback = defaults.get(tool_name)

        try:
            from antigona.gateway.tool_gateway import get_gateway

            config, use_gateway = get_gateway().resolve(tool_name)
        except Exception as exc:
            logger.warning("Tool gateway unavailable for %r: %s", tool_name, exc)
            return fallback

        if not use_gateway:
            return fallback

        backend = implementations.get(tool_name, {}).get(config.backend)
        if backend is None:
            logger.warning(
                "Backend %r for tool %r is not implemented; falling back to default.",
                config.backend, tool_name,
            )
            return fallback
        return backend

    # ── WRITE_FILE ───────────────────────────────────────────────────────────

    async def _execute_write_file(self, action: Action, **kwargs: Any) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))

        try:
            enforce_write_fence(kwargs.get("ownership") or getattr(self, "ownership", None), "action_executor.write_file")
        except FenceDeniedError as exc:
            res = ActionResult(
                success=False, action_type=ActionType.WRITE_FILE,
                message=f"Ошибка владения: {exc.check.reason}",
                path=action.path, error=f"protected write denied: {exc.check.reason}",
            )
            self.audit_logger.log_action(
                channel=channel,
                user_id=user_id,
                session_id=session_id,
                command=f"WRITE_FILE {action.path}",
                exit_code=403,
                status="DENIED",
            )
            return res

        # A-CORE-001/A-00: this executor parses LLM output, so it is a model
        # write surface.  The single writable root is the canonical workspace;
        # an ABSOLUTE target outside it (or a relative target that escapes it)
        # is refused with no side effect.  This surface has no approval-grant
        # plumbing, so it cannot honour a grant — fail closed instead of writing
        # "as-is" (the owner-session risk gate below only covers HIGH, and an
        # out-of-workspace target used to classify MEDIUM).
        from antigona.security.risk_classifier import resolve_confined_workspace_path

        confined = resolve_confined_workspace_path(action.path)
        if confined is None:
            res = ActionResult(
                success=False,
                action_type=ActionType.WRITE_FILE,
                message=(
                    "🚫 Запись вне workspace отклонена: требуется одобрение владельца "
                    f"({action.path})."
                ),
                path=action.path,
                error="APPROVAL_REQUIRED_OUT_OF_WORKSPACE",
            )
            self.audit_logger.log_action(
                channel=channel,
                user_id=user_id,
                session_id=session_id,
                command=f"WRITE_FILE {action.path}",
                exit_code=403,
                status="DENIED",
                details={"reason": "OUT_OF_WORKSPACE_WRITE_REQUIRES_APPROVAL"},
            )
            return res

        # Single writable root: the canonical workspace, for relative and
        # absolute notation alike (a bare relative path used to be written
        # relative to the process CWD, i.e. into the code tree).
        path = confined
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(action.content, encoding="utf-8")
            res = ActionResult(
                success=True, action_type=ActionType.WRITE_FILE,
                message=f"Файл {action.path} создан ({path.stat().st_size} байт).",
                path=action.path,
            )
            exit_code = 0
            status = "SUCCESS"
        except Exception as e:
            res = ActionResult(
                success=False, action_type=ActionType.WRITE_FILE,
                message=f"Ошибка создания файла {action.path}: {e}",
                path=action.path, error=str(e),
            )
            exit_code = 1
            status = "FAILED"

        self.audit_logger.log_action(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            command=f"WRITE_FILE {action.path}",
            exit_code=exit_code,
            status=status,
        )

        return res

    # ── SEND_FILE ────────────────────────────────────────────────────────────

    async def _execute_send_file(self, action: Action, **kwargs: Any) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))

        path = Path(action.path)
        if not path.exists():
            alt = paths.project_root() / action.path
            if alt.exists():
                path = alt
            else:
                res = ActionResult(
                    success=False, action_type=ActionType.SEND_FILE,
                    message=f"Файл {action.path} не найден.", error="File not found",
                )
                self.audit_logger.log_action(channel, user_id, session_id, f"SEND_FILE {action.path}", 1, status="FAILED")
                return res

        if not path.is_file():
            res = ActionResult(
                success=False, action_type=ActionType.SEND_FILE,
                message=f"{action.path} не является файлом.", error="Not a file",
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"SEND_FILE {action.path}", 1, status="FAILED")
            return res

        message = kwargs.get("message")
        if message is not None:
            try:
                from aiogram.types import FSInputFile
                await message.answer_document(
                    document=FSInputFile(str(path)),
                    caption=f"📄 {path.name}",
                )
            except Exception as e:
                res = ActionResult(
                    success=False, action_type=ActionType.SEND_FILE,
                    message=f"Ошибка отправки {action.path}: {e}",
                    path=action.path, error=str(e),
                )
                self.audit_logger.log_action(channel, user_id, session_id, f"SEND_FILE {action.path}", 1, status="FAILED")
                return res

        if message is None:
            try:
                adapter = self._get_delivery_adapter()
                adapter.send_file(path=str(path), caption=f"📄 {path.name}")
            except Exception as e:
                res = ActionResult(
                    success=False, action_type=ActionType.SEND_FILE,
                    message=f"Ошибка отправки {action.path}: {e}",
                    path=action.path, error=str(e),
                )
                self.audit_logger.log_action(channel, user_id, session_id, f"SEND_FILE {action.path}", 1, status="FAILED")
                return res

            res = ActionResult(
                success=True, action_type=ActionType.SEND_FILE,
                message=f"Файл {action.path} отправлен в Telegram.",
                path=action.path,
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"SEND_FILE {action.path}", 0, status="SUCCESS")
            return res

        res = ActionResult(
            success=True, action_type=ActionType.SEND_FILE,
            message=f"Файл {action.path} отправлен в Telegram.",
            path=action.path,
        )
        self.audit_logger.log_action(channel, user_id, session_id, f"SEND_FILE {action.path}", 0, status="SUCCESS")
        return res

    # ── RUN_SHELL ────────────────────────────────────────────────────────────

    async def _execute_run_shell(self, action: Action, **kwargs: Any) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))
        timeout = float(kwargs.get("timeout", 30.0))

        status = "SUCCESS"
        exit_code = 0

        try:
            proc = await asyncio.create_subprocess_shell(
                action.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # CRITICAL: run the shell in its OWN process group so that a
                # timeout killpg() can never hit the parent's group (pytest /
                # gateway). Without this, create_subprocess_shell inherits the
                # parent's PGID and killpg() kills the whole suite (SIGKILL -9
                # on the test process at ~73%).
                process_group=0,
            )
            self._running_processes[session_id] = proc
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                output = stdout.decode(errors="replace").strip()
                err = stderr.decode(errors="replace").strip()
                exit_code = proc.returncode if proc.returncode is not None else 0

                is_cancelled = session_id in self._cancelled_sessions
                if is_cancelled:
                    self._cancelled_sessions.discard(session_id)

                if is_cancelled or exit_code < 0 or exit_code in (-15, -9, 9, 15, 130, 137):
                    exit_code = 130
                    status = "CANCELLED"
                    msg = f"Выполнение отменено пользователем (/cancel).\nЧастичный вывод:\n{output or err}"
                    res = ActionResult(
                        success=False,
                        action_type=ActionType.RUN_SHELL,
                        message=msg,
                        error="CANCELLED",
                    )
                elif exit_code != 0:
                    status = "FAILED"
                    res = ActionResult(
                        success=False,
                        action_type=ActionType.RUN_SHELL,
                        message=err or output or f"Exit code {exit_code}",
                        error=err or f"Exit code {exit_code}",
                    )
                else:
                    status = "SUCCESS"
                    res = ActionResult(
                        success=True,
                        action_type=ActionType.RUN_SHELL,
                        message=output[:500] if output else "OK",
                    )
            except TimeoutError:
                is_cancelled = session_id in self._cancelled_sessions
                if is_cancelled:
                    self._cancelled_sessions.discard(session_id)
                try:
                    import os
                    import signal
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        proc.kill()
                except Exception:
                    pass
                try:
                    stdout, stderr = await proc.communicate()
                    output = stdout.decode(errors="replace").strip()
                    err = stderr.decode(errors="replace").strip()
                except Exception:
                    output, err = "", ""

                if is_cancelled:
                    exit_code = 130
                    status = "CANCELLED"
                    msg = f"Выполнение отменено пользователем (/cancel).\nЧастичный вывод:\n{output or err}"
                    res = ActionResult(
                        success=False,
                        action_type=ActionType.RUN_SHELL,
                        message=msg,
                        error="CANCELLED",
                    )
                else:
                    exit_code = 124
                    status = "TIMEOUT"
                    msg = f"Команда превысила таймаут {timeout}с.\nЧастичный вывод:\n{output or err}"
                    res = ActionResult(
                        success=False,
                        action_type=ActionType.RUN_SHELL,
                        message=msg,
                        error="TIMEOUT",
                    )
            except asyncio.CancelledError:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout, stderr = await proc.communicate()
                output = stdout.decode(errors="replace").strip()
                exit_code = 130
                status = "CANCELLED"
                msg = f"Выполнение отменено по команде /cancel.\nЧастичный вывод:\n{output}"
                res = ActionResult(
                    success=False,
                    action_type=ActionType.RUN_SHELL,
                    message=msg,
                    error="CANCELLED",
                )
        except Exception as e:
            exit_code = 1
            status = "FAILED"
            res = ActionResult(
                success=False,
                action_type=ActionType.RUN_SHELL,
                message=str(e),
                error=str(e),
            )
        finally:
            self._running_processes.pop(session_id, None)

        self.audit_logger.log_action(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            command=action.command,
            exit_code=exit_code,
            status=status,
        )

        return res

    # ── CONFIGURE_KEY ────────────────────────────────────────────────────────

    async def _execute_configure_key(self, action: Action, **kwargs: Any) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))

        provider = action.path
        api_key = action.content
        from antigona.tools.key_manager import configure_full_keyflow

        result = configure_full_keyflow(provider, api_key)
        if result["success"]:
            res = ActionResult(
                success=True, action_type=ActionType.CONFIGURE_KEY,
                message=f"✅ Ключ {provider} записан и проверен. Работает.",
            )
            exit_code = 0
            status = "SUCCESS"
        else:
            err_msg = result.get("error", "Ошибка настройки ключа")
            res = ActionResult(
                success=False, action_type=ActionType.CONFIGURE_KEY,
                message=f"❌ Настройка ключа не удалась: {err_msg}",
                error=err_msg,
            )
            exit_code = 1
            status = "FAILED"

        self.audit_logger.log_action(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            command=f"CONFIGURE_KEY provider={provider}",
            exit_code=exit_code,
            status=status,
        )
        return res

    # ── GENERATE_IMAGE ────────────────────────────────────────────────────────

    async def _execute_generate_image(
        self, action: Action, **kwargs: Any
    ) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))
        prompt = action.content

        if not prompt:
            res = ActionResult(
                success=False, action_type=ActionType.GENERATE_IMAGE,
                message="Промпт пустой.", error="Empty prompt",
            )
            self.audit_logger.log_action(channel, user_id, session_id, "GENERATE_IMAGE", 1, status="FAILED")
            return res

        try:
            from antigona.tools.image_gen import ImageGenerator

            generator_cls = self._get_backend_for("image_gen") or ImageGenerator
            generator = generator_cls()
            message = kwargs.get("message")
            filepath = await generator.generate_and_send(
                prompt=prompt,
                message=message,
            )

            msg = f"✅ Изображение сгенерировано: {prompt[:60]}"
            res = ActionResult(
                success=True,
                action_type=ActionType.GENERATE_IMAGE,
                message=msg,
                path=filepath,
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"GENERATE_IMAGE prompt='{prompt[:40]}'", 0, status="SUCCESS")
            return res
        except Exception as e:
            res = ActionResult(
                success=False,
                action_type=ActionType.GENERATE_IMAGE,
                message=f"Ошибка генерации изображения: {e}",
                error=str(e),
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"GENERATE_IMAGE prompt='{prompt[:40]}'", 1, status="FAILED")
            return res

    # ── MEMORIZE ──────────────────────────────────────────────────────────────

    async def _execute_memorize(self, action: Action, **kwargs: Any) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))

        store = action.metadata.get("store", action.path) or "memory"
        title = action.metadata.get("title", action.content[:50].strip())
        content = action.content

        if not content:
            res = ActionResult(
                success=False, action_type=ActionType.MEMORIZE,
                message="Пустое содержимое для запоминания.", error="Empty content",
            )
            self.audit_logger.log_action(channel, user_id, session_id, "MEMORIZE", 1, status="FAILED")
            return res

        try:
            from antigona.memory.file_memory import FileMemory

            memory = FileMemory()
            current = memory.get_content(store)
            estimated_new = len(current) + len(title) + len(content) + 10
            limit = 2200 if store == "memory" else 1375
            if estimated_new > limit:
                res = ActionResult(
                    success=False, action_type=ActionType.MEMORIZE,
                    message=f"Память {store.upper()} переполнена (лимит {limit} chars).",
                    error="Overflow",
                )
                self.audit_logger.log_action(channel, user_id, session_id, f"MEMORIZE store={store}", 1, status="FAILED")
                return res

            memory.add_entry(store, title, content)
            res = ActionResult(
                success=True, action_type=ActionType.MEMORIZE,
                message=f"💾 {store.title()}: {title}",
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"MEMORIZE store={store} title='{title}'", 0, status="SUCCESS")
            return res
        except Exception as e:
            res = ActionResult(
                success=False, action_type=ActionType.MEMORIZE,
                message=f"Ошибка сохранения памяти: {e}",
                error=str(e),
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"MEMORIZE store={store}", 1, status="FAILED")
            return res

    # ── READ_FILE ──────────────────────────────────────────────────────────────

    async def _execute_read_file(
        self, action: Action, **kwargs: Any
    ) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))

        path = Path(action.path)
        if not path.exists():
            alt = paths.project_root() / action.path
            if alt.exists():
                path = alt
            else:
                res = ActionResult(
                    success=False, action_type=ActionType.READ_FILE,
                    message=f"Файл {action.path} не найден.",
                    error="File not found",
                )
                self.audit_logger.log_action(channel, user_id, session_id, f"READ_FILE {action.path}", 1, status="FAILED")
                return res

        if not path.is_file():
            res = ActionResult(
                success=False, action_type=ActionType.READ_FILE,
                message=f"{action.path} не является файлом.",
                error="Not a file",
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"READ_FILE {action.path}", 1, status="FAILED")
            return res

        try:
            content = path.read_text(errors="replace")
            size = path.stat().st_size
            preview = content[:3000]
            if len(content) > 3000:
                preview += f"\n\n... [файл обрезан, полный размер {size} байт]"
            message = f"📄 {action.path} ({size} байт)\n```\n{preview}\n```"
            res = ActionResult(
                success=True, action_type=ActionType.READ_FILE,
                message=message,
                path=action.path,
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"READ_FILE {action.path}", 0, status="SUCCESS")
            return res
        except Exception as e:
            res = ActionResult(
                success=False, action_type=ActionType.READ_FILE,
                message=f"Ошибка чтения файла {action.path}: {e}",
                error=str(e),
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"READ_FILE {action.path}", 1, status="FAILED")
            return res

    # ── SEARCH_FILES ───────────────────────────────────────────────────────────

    async def _execute_search_files(
        self, action: Action, **kwargs: Any
    ) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))

        import subprocess

        pattern = action.content
        search_path = action.path or str(paths.project_root())
        has_regex_chars = bool(re.search(r"[\[\]\.\^\\\+\(\)\{\}]", pattern))
        timeout = 15

        try:
            if has_regex_chars or pattern.startswith("."):
                cmd = ["grep", "-r", "--include=*.py", "--include=*.md",
                       "--include=*.txt", "--include=*.json", "--include=*.yaml",
                       "--include=*.yml", "--include=*.toml", "--include=*.cfg",
                       "--include=*.ini", "--include=*.sh",
                       "-l", pattern, search_path]
            else:
                cmd = ["find", search_path, "-maxdepth", "5",
                       "-type", "f", "-name", pattern]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            output = result.stdout.strip()
            if not output:
                res = ActionResult(
                    success=True, action_type=ActionType.SEARCH_FILES,
                    message=f"🔍 По паттерну '{pattern}' ничего не найдено.",
                )
                self.audit_logger.log_action(channel, user_id, session_id, f"SEARCH_FILES {pattern}", 0, status="SUCCESS")
                return res

            lines = output.split("\n")
            if len(lines) > 30:
                summary = "\n".join(lines[:30])
                message = (
                    f"🔍 Найдено {len(lines)} файлов по '{pattern}':\n"
                    f"{summary}\n... и ещё {len(lines) - 30}"
                )
            else:
                message = f"🔍 Найдено {len(lines)} файлов по '{pattern}':\n{output}"

            res = ActionResult(
                success=True, action_type=ActionType.SEARCH_FILES,
                message=message,
                path=search_path,
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"SEARCH_FILES {pattern}", 0, status="SUCCESS")
            return res
        except subprocess.TimeoutExpired:
            res = ActionResult(
                success=False, action_type=ActionType.SEARCH_FILES,
                message=f"Поиск превысил таймаут ({timeout}с) для паттерна '{pattern}'.",
                error="Timeout",
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"SEARCH_FILES {pattern}", 124, status="TIMEOUT")
            return res
        except Exception as e:
            res = ActionResult(
                success=False, action_type=ActionType.SEARCH_FILES,
                message=f"Ошибка поиска: {e}",
                error=str(e),
            )
            self.audit_logger.log_action(channel, user_id, session_id, f"SEARCH_FILES {pattern}", 1, status="FAILED")
            return res

    # ── RUN_CODE ───────────────────────────────────────────────────────────────

    async def _execute_run_code(
        self, action: Action, **kwargs: Any
    ) -> ActionResult:
        channel = str(kwargs.get("channel", "cli"))
        user_id = str(kwargs.get("user_id", "owner"))
        session_id = str(kwargs.get("session_id", "default_session"))
        timeout = float(kwargs.get("timeout", 30.0))

        language = action.path.lower().strip()
        code = action.content

        if not code:
            res = ActionResult(
                success=False, action_type=ActionType.RUN_CODE,
                message="Код пустой.", error="Empty code",
            )
            self.audit_logger.log_action(channel, user_id, session_id, "RUN_CODE", 1, status="FAILED")
            return res

        status = "SUCCESS"
        exit_code = 0

        try:
            if language in ("python", "py"):
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # Same process-group isolation as RUN_SHELL / bash RUN_CODE:
                    # the timeout killpg() below must only kill the child, never
                    # the parent's group (pytest / gateway). Without this the
                    # python branch inherits the parent PGID and a hung
                    # RUN_CODE|python kills the whole test suite.
                    process_group=0,
                )
            elif language in ("bash", "sh"):
                proc = await asyncio.create_subprocess_shell(
                    code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # Same process-group isolation as RUN_SHELL: timeout
                    # killpg() must only kill the child shell, never the
                    # parent's group (pytest / gateway).
                    process_group=0,
                )
            else:
                res = ActionResult(
                    success=False, action_type=ActionType.RUN_CODE,
                    message=f"Неподдерживаемый язык: {language}. Используй python или bash.",
                    error=f"Unsupported language: {language}",
                )
                self.audit_logger.log_action(channel, user_id, session_id, f"RUN_CODE lang={language}", 1, status="FAILED")
                return res

            self._running_processes[session_id] = proc
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                output = stdout.decode(errors="replace").strip()
                err = stderr.decode(errors="replace").strip()
                exit_code = proc.returncode if proc.returncode is not None else 0

                is_cancelled = session_id in self._cancelled_sessions
                if is_cancelled:
                    self._cancelled_sessions.discard(session_id)

                if is_cancelled or exit_code < 0 or exit_code in (-15, -9, 9, 15, 130, 137):
                    exit_code = 130
                    status = "CANCELLED"
                    res = ActionResult(
                        success=False, action_type=ActionType.RUN_CODE,
                        message=f"Выполнение кода отменено (/cancel).\nЧастичный вывод:\n{output or err}",
                        error="CANCELLED",
                    )
                elif exit_code != 0:
                    status = "FAILED"
                    res = ActionResult(
                        success=False, action_type=ActionType.RUN_CODE,
                        message=f"❌ Код завершился с кодом {exit_code}\n{err}",
                        error=err or f"Exit code {exit_code}",
                    )
                else:
                    status = "SUCCESS"
                    res = ActionResult(
                        success=True, action_type=ActionType.RUN_CODE,
                        message=output[:2000] if output else "✅ Код выполнен успешно.",
                    )
            except TimeoutError:
                is_cancelled = session_id in self._cancelled_sessions
                if is_cancelled:
                    self._cancelled_sessions.discard(session_id)
                try:
                    import os
                    import signal
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        proc.kill()
                except Exception:
                    pass
                try:
                    stdout, stderr = await proc.communicate()
                    output = stdout.decode(errors="replace").strip()
                    err = stderr.decode(errors="replace").strip()
                except Exception:
                    output, err = "", ""

                if is_cancelled:
                    exit_code = 130
                    status = "CANCELLED"
                    res = ActionResult(
                        success=False, action_type=ActionType.RUN_CODE,
                        message=f"Выполнение кода отменено (/cancel).\nЧастичный вывод:\n{output or err}",
                        error="CANCELLED",
                    )
                else:
                    exit_code = 124
                    status = "TIMEOUT"
                    res = ActionResult(
                        success=False, action_type=ActionType.RUN_CODE,
                        message=f"Код превысил таймаут {timeout}с.\nЧастичный вывод:\n{output or err}",
                        error="TIMEOUT",
                    )
            except asyncio.CancelledError:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout, stderr = await proc.communicate()
                output = stdout.decode(errors="replace").strip()
                exit_code = 130
                status = "CANCELLED"
                res = ActionResult(
                    success=False, action_type=ActionType.RUN_CODE,
                    message=f"Выполнение отменено по команде /cancel.\nЧастичный вывод:\n{output}",
                    error="CANCELLED",
                )
        except Exception as e:
            exit_code = 1
            status = "FAILED"
            res = ActionResult(
                success=False, action_type=ActionType.RUN_CODE,
                message=f"Ошибка выполнения кода: {e}",
                error=str(e),
            )
        finally:
            self._running_processes.pop(session_id, None)

        self.audit_logger.log_action(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            command=f"RUN_CODE lang={language}",
            exit_code=exit_code,
            status=status,
        )

        return res
