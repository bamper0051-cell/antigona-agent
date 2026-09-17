"""PolicyEngine — Проверка безопасности действий Antigona и интеграция с OwnerOverride.

Обеспечивает:
- Интеграцию с OwnerOverrideManager.
- Классификацию действий на SAFE, SENSITIVE, CRITICAL.
- Снятие глухих отказов политики для SAFE и SENSITIVE в режиме активного Owner Override (channel, user_id, session_id).
- 2-ступенчатое целевое подтверждение для CRITICAL-действий с точным fenced code block, ожидаемым эффектом,
  подтверждающей фразой или 1-разовым токеном `/confirm <token>`.
- Отклонение простых "да" / "yes" / "ok" и несовпадающих токенов.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from antigona.core.paths import home_dir
from antigona.security.elevation import ElevationAuthority, owner_elevation_authority, principal_for
from antigona.security.owner_override import OwnerOverrideManager
from antigona.security.risk_classifier import RiskClassifier, RiskLevel

logger = logging.getLogger(__name__)


class ActionCategory(StrEnum):
    """Категория безопасности действия."""

    SAFE = "SAFE"
    SENSITIVE = "SENSITIVE"
    CRITICAL = "CRITICAL"


# A pending CRITICAL-action confirmation expires after this many seconds.
# Expired confirmations are dropped so an ignored prompt can't be confirmed
# later by accident, and the pending dict is bounded to prevent growth.
CONFIRMATION_TTL: float = 300.0
MAX_PENDING_CONFIRMATIONS: int = 100

#: Token -> owning PolicyEngine for every live CRITICAL confirmation in this
#: process.  A surface (Telegram ``/confirm``, CLI) receives only the token, not
#: the engine instance that issued it, so the canonical APPROVAL → RE-DISPATCH
#: bridge would otherwise be unreachable (defect A-4: ``/confirm`` resolved
#: nothing).  Entries are removed as soon as the confirmation is consumed or
#: expires, so this never grows beyond ``MAX_PENDING_CONFIRMATIONS`` per engine.
_GLOBAL_PENDING: dict[str, PolicyEngine] = {}


def normalize_grant_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Canonical argument view an approval grant is bound to.

    Dispatch metadata (underscore keys) and the approval token itself are not
    part of the approved action: the token is the proof, not an argument, and
    including it would make the digest un-recomputable at RE-DISPATCH time.
    Everything else is kept verbatim, so the binding stays exact.
    """
    reserved = {
        "actor",
        "approval_token",
        "auth_context",
        "channel",
        "correlation_id",
        "owner_id",
        "requester",
        "session_id",
        "token",
        "turn_id",
        "user_id",
    }
    return {
        k: v
        for k, v in (args or {}).items()
        if not str(k).startswith("_") and str(k) not in reserved
    }


def _extract_candidate_token(raw: str) -> str:
    """Pull the one-shot token out of ``/confirm <token>`` (or a bare token)."""
    text = raw.strip()
    lowered = text.lower()
    if lowered.startswith("/confirm"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
    if len(text) == 8 and text.isalnum():
        return text
    return ""


@dataclass
class PendingConfirmation:
    """Заявка на 2-ступенчатое подтверждение CRITICAL-действия."""

    token: str
    exact_phrase: str
    command: str
    expected_effect: str
    channel: str
    user_id: str
    session_id: str
    created_at: float
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    grant_token: str = ""

    def format_prompt(self) -> str:
        """Сформировать человекочитаемый текст запроса 2-ступенчатого подтверждения."""
        return (
            "🚨 **КРИТИЧЕСКОЕ ДЕЙСТВИЕ ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ**\n\n"
            "**Команда к выполнению:**\n"
            f"```\n{self.command}\n```\n\n"
            f"**Ожидаемый эффект:** {self.expected_effect}\n\n"
            "Для выполнения подтвердите операцию одним из двух способов:\n"
            f'1. Отправьте точную фразу: `{self.exact_phrase}`\n'
            f"2. Или отправьте одноразовый токен: `/confirm {self.token}`\n\n"
            "⚠️ Простые варианты 'да'/'yes'/'ok' отклоняются."
        )


class PolicyEngine:
    """Движок политик безопасности.

    Проверяет выполняемые действия на соответствие политикам и интеграцию с OwnerOverride.
    """

    def __init__(
        self,
        owner_override: OwnerOverrideManager | None = None,
        require_approval: bool = True,
        grant_store: Any | None = None,
        elevation: ElevationAuthority | None = None,
    ) -> None:
        self.owner_override = owner_override
        # Campaign CP-7: when no OwnerOverrideManager is injected, the elevation
        # check reads the canonical shared ElevationAuthority directly instead of
        # being inert. ``elevation`` overrides the default store (tests).
        self._elevation = elevation
        self.require_approval = require_approval
        self.grant_store = grant_store
        self.risk_classifier = RiskClassifier()
        self._pending_confirmations: dict[str, PendingConfirmation] = {}

    def _is_elevated(self, channel: str, user_id: str, session_id: str) -> bool:
        """Is this principal in an active elevated session? (CP-7 wiring)

        An explicitly injected ``OwnerOverrideManager`` is authoritative and its
        errors are allowed to reach :meth:`check`'s fail-closed boundary (DENY) —
        a broken override must never silently ALLOW. With no manager, the shared
        canonical :class:`ElevationAuthority` is read (it is itself fail-closed to
        ``False`` on any storage error).
        """
        if self.owner_override is not None:
            return bool(self.owner_override.is_elevated(channel, user_id, session_id))
        store = self._elevation or owner_elevation_authority()
        return bool(store.is_elevated(principal_for(channel, user_id, session_id)))

    def classify_action_category(
        self, action_type: str, command: str = "", path: str = ""
    ) -> ActionCategory:
        """Определить категорию действия: SAFE, SENSITIVE, CRITICAL."""
        risk_level = self.risk_classifier.classify(
            action_type=action_type, path=path, content=command
        )

        if risk_level == RiskLevel.CRITICAL:
            return ActionCategory.CRITICAL

        if risk_level == RiskLevel.HIGH:
            return ActionCategory.SENSITIVE

        if not self.risk_classifier._is_write_action(action_type):
            cmd_lower = command.lower().strip()
            critical_indicators = [
                "rm -rf",
                "rm -fr",
                "rm -r -f",
                "drop database",
                "dropdb",
                "truncate table",
                "systemctl stop",
                "systemctl restart",
                "systemctl disable",
                "service stop",
                "service restart",
                "iptables",
                "nftables",
                "ufw",
                "firewall-cmd",
                "kill -9",
                "pkill -9",
                "pkill -f",
                "killall",
            ]
            if any(ind in cmd_lower for ind in critical_indicators):
                return ActionCategory.CRITICAL

        act_upper = action_type.upper().strip().replace(".", "_").replace("-", "_")
        if act_upper in (
            "READ_FILE",
            "READ_TEXT",
            "WORKSPACE_READ_TEXT",
            "FILE_READ",
            "FILESYSTEM_READ",
            "SEARCH_FILES",
            "WEB_SEARCH",
            "GENERATE_IMAGE",
            "MEMORIZE",
        ):
            return ActionCategory.SAFE

        if risk_level == RiskLevel.LOW:
            return ActionCategory.SAFE

        return ActionCategory.SENSITIVE

    def _determine_expected_effect(self, command: str, path: str = "") -> tuple[str, str]:
        """Определить описание ожидаемого эффекта и exact_phrase для команды/пути.

        Returns:
            (expected_effect, exact_phrase)
        """
        cmd = command.strip()
        cmd_lower = cmd.lower()

        if "rm -rf" in cmd_lower or "rm -fr" in cmd_lower or "rm -r -f" in cmd_lower:
            target = path or (cmd.split()[-1] if len(cmd.split()) > 1 else str(home_dir() / "target"))
            effect = f"Опасное удаление директории/файлов по пути: {target}"
            phrase = f"Подтверждаю удаление {target}"
            return effect, phrase

        if "drop database" in cmd_lower or "dropdb" in cmd_lower:
            target = path or (cmd.split()[-1] if len(cmd.split()) > 1 else "target_db")
            effect = f"Полное и необратимое удаление базы данных: {target}"
            phrase = f"Подтверждаю удаление базы данных {target}"
            return effect, phrase

        if any(k in cmd_lower for k in ("systemctl stop", "systemctl restart", "service stop", "service restart")):
            target = path or (cmd.split()[-1] if len(cmd.split()) > 1 else "service")
            effect = f"Остановка или перезапуск системного сервиса: {target}"
            phrase = f"Подтверждаю изменение сервиса {target}"
            return effect, phrase

        if any(fw in cmd_lower for fw in ("iptables", "ufw", "nftables", "firewall-cmd")):
            effect = f"Изменение правил сетевого экрана/файрвола: {cmd}"
            phrase = f"Подтверждаю изменение файрвола {cmd[:30]}"
            return effect, phrase

        if any(k in cmd_lower for k in ("kill -9", "pkill -9", "pkill -f", "killall")):
            effect = f"Принудительное завершение системных процессов: {cmd}"
            phrase = f"Подтверждаю завершение процессов {cmd[:30]}"
            return effect, phrase

        target = path or cmd[:40]
        effect = f"Выполнение критической операции: {cmd}"
        phrase = f"Подтверждаю выполнение {target}"
        return effect, phrase

    def create_2step_confirmation(
        self,
        channel: str,
        user_id: str | int,
        session_id: str,
        command: str,
        path: str = "",
        tool_name: str = "",
        args: dict[str, Any] | None = None,
    ) -> PendingConfirmation:
        """Создать заявку на 2-ступенчатое подтверждение CRITICAL-действия."""
        token = secrets.token_urlsafe(16)
        effect, exact_phrase = self._determine_expected_effect(command, path)

        pending = PendingConfirmation(
            token=token,
            exact_phrase=exact_phrase,
            command=command,
            expected_effect=effect,
            channel=str(channel),
            user_id=str(user_id),
            session_id=str(session_id),
            created_at=time.time(),
            tool_name=tool_name,
            args=dict(args or {}),
        )

        self._prune_expired()
        self._pending_confirmations[token] = pending
        _GLOBAL_PENDING[token] = self
        if len(self._pending_confirmations) > MAX_PENDING_CONFIRMATIONS:
            # Bounded growth: drop oldest (dicts preserve insertion order).
            for old_tok in list(self._pending_confirmations)[: len(self._pending_confirmations) - MAX_PENDING_CONFIRMATIONS]:
                self._pending_confirmations.pop(old_tok, None)
                _GLOBAL_PENDING.pop(old_tok, None)
        return pending

    def _forget(self, token: str) -> None:
        """Drop a token from both the engine-local and the process registry."""
        self._pending_confirmations.pop(token, None)
        if _GLOBAL_PENDING.get(token) is self:
            _GLOBAL_PENDING.pop(token, None)

    def _prune_expired(self) -> None:
        """Drop pending confirmations older than ``CONFIRMATION_TTL`` seconds."""
        now = time.time()
        expired = [
            tok
            for tok, p in self._pending_confirmations.items()
            if (now - getattr(p, "created_at", now)) > CONFIRMATION_TTL
        ]
        for tok in expired:
            self._forget(tok)

    def _get_grant_store(self) -> Any:
        """Return the durable ApprovalGrantStore, creating it on first use."""
        if self.grant_store is None:
            from antigona.security.approval_grant import ApprovalGrantStore

            self.grant_store = ApprovalGrantStore()
        return self.grant_store

    def _issue_grant_for(
        self, pending: PendingConfirmation, user_id: str | int
    ) -> None:
        """Issue the durable one-shot grant ONLY when the owner confirms.

        Canonical REQUEST → POLICY → APPROVAL → RE-DISPATCH model: the grant
        is minted at APPROVAL time (not at request time) and consumed exactly
        once at RE-DISPATCH. Bound to actor + tool + exact args digest and
        persisted so a replay after restart cannot re-authorize the call.
        A failure to mint degrades to the in-memory path (pending.grant_token
        stays empty) — the RE-DISPATCH bridge then fails closed.
        """
        if not pending.tool_name:
            return
        try:
            raw = self._get_grant_store().issue(
                actor=str(user_id),
                tool_name=pending.tool_name,
                args=normalize_grant_args(pending.args),
                issuer="policy-engine",
                channel=pending.channel,
                session_id=pending.session_id,
                reason=pending.command or pending.tool_name,
            )
            pending.grant_token = raw
        except Exception as exc:
            logger.warning(
                "durable grant issue failed for tool=%s (in-memory only): %s",
                pending.tool_name,
                exc,
            )

    def verify_confirmation(
        self,
        channel: str,
        user_id: str | int,
        session_id: str,
        user_input: str,
    ) -> tuple[bool, str, PendingConfirmation | None]:
        """Проверить подтверждение от пользователя."""
        self._prune_expired()
        raw = user_input.strip()
        raw_lower = raw.lower()

        simple_disallowed = {"да", "yes", "ok", "y", "1", "sure", "confirm", "подтверждаю"}
        if raw_lower in simple_disallowed:
            return (
                False,
                "🚫 Отклонено: простые ответы ('да'/'ok') не принимаются. Введите точную фразу или /confirm <token>.",
                None,
            )

        candidate_token = _extract_candidate_token(raw)

        if candidate_token and candidate_token in self._pending_confirmations:
            pending = self._pending_confirmations.pop(candidate_token)
            self._forget(candidate_token)
            if (
                pending.channel == str(channel)
                and pending.user_id == str(user_id)
                and pending.session_id == str(session_id)
            ):
                self._issue_grant_for(pending, user_id)
                return (True, f"✅ Действие по токену {candidate_token} успешно подтверждено.", pending)
            # Identity mismatch: the confirmation is NOT consumed — restore it
            # in both registries so the rightful owner can still confirm.
            self._pending_confirmations[candidate_token] = pending
            _GLOBAL_PENDING[candidate_token] = self

        matched_token: str | None = None
        matched_pending: PendingConfirmation | None = None

        for tok, p in self._pending_confirmations.items():
            if (
                p.channel == str(channel)
                and p.user_id == str(user_id)
                and p.session_id == str(session_id)
            ):
                if raw == p.exact_phrase:
                    matched_token = tok
                    matched_pending = p
                    break

        if matched_token and matched_pending:
            self._forget(matched_token)
            self._issue_grant_for(matched_pending, user_id)
            return (
                True,
                f"✅ Действие '{matched_pending.command}' подтверждено точной фразой.",
                matched_pending,
            )

        return (
            False,
            "❌ Отклонено: несовпадающая подтверждающая фраза или токен.",
            None,
        )

    async def confirm_and_continue(
        self,
        channel: str,
        user_id: str | int,
        session_id: str,
        user_input: str,
        *,
        tool_name: str = "",
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Canonical RE-DISPATCH bridge (Milestone 0 scope 2).

        After REQUEST→POLICY→APPROVAL (``check`` + ``verify_confirmation``),
        the surface calls this with the owner's confirmation to re-dispatch
        the SAME action. It consumes the one-shot grant exactly once; a second
        call with the same grant token (replay, forged, re-used) is DENIED.
        No grant was issued (missing tool_name or mint failure) => fail closed.
        """
        ok, msg, pending = self.verify_confirmation(
            channel, user_id, session_id, user_input
        )
        if not ok:
            return {"allowed": False, "reason": msg}
        if pending is None or not getattr(pending, "grant_token", ""):
            return {
                "allowed": False,
                "reason": "Approval confirmed but no durable grant issued; denied (fail-closed).",
            }
        verdict = self._get_grant_store().verify_and_consume(
            pending.grant_token,
            actor=str(user_id),
            tool_name=tool_name or pending.tool_name,
            args=normalize_grant_args(args if args is not None else pending.args),
            consumed_by=f"redispath:{user_id}",
        )
        if not verdict.valid:
            reason = (
                verdict.reason.value if verdict.reason is not None else "invalid"
            )
            return {
                "allowed": False,
                "reason": f"Грант отклонён (fail-closed, one-shot): {reason}.",
            }
        return {
            "allowed": True,
            "reason": "Approved: одноразовый grant подтверждён; действие может выполниться.",
            "grant_id": verdict.grant_id,
        }

    def pending_for_token(self, token: str) -> PendingConfirmation | None:
        """Return the live pending confirmation for *token* (no consumption)."""
        self._prune_expired()
        return self._pending_confirmations.get(token)

    async def check(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Проверить, разрешено ли действие по политике безопасности.

        Fail-closed: любой внутренний сбой (исключение в классификации,
        owner-override, и т.п.) приводит к DENY / INTERNAL_ERROR — никогда
        не ALLOW. Никакой exception не может «продавить» выполнение.
        """
        try:
            return await self._check_impl(action, params=params, context=context)
        except Exception as exc:  # noqa: BLE001 — fail-closed boundary
            logger.exception("PolicyEngine.check failed (action=%s): %s", action, exc)
            return {
                "allowed": False,
                "requires_approval": True,
                "requires_2step_confirmation": False,
                "category": "UNKNOWN",
                "risk_level": "UNKNOWN",
                "reason": (
                    "INTERNAL_ERROR: policy evaluation failed — action denied (fail-closed)."
                ),
                "error": f"POLICY_INTERNAL_ERROR: {type(exc).__name__}",
            }

    async def _check_impl(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Внутренняя реализация проверки политики (см. :meth:`check`)."""
        params = params or {}
        context = context or {}

        channel = str(context.get("channel", "cli"))
        user_id = context.get("user_id")
        if user_id is None:
            return {
                "allowed": False,
                "requires_approval": True,
                "requires_2step_confirmation": False,
                "category": "UNKNOWN",
                "risk_level": "UNKNOWN",
                "reason": "missing user_id in context (fail-closed)",
            }
        user_id = str(user_id)
        session_id = str(context.get("session_id", "default"))
        command = str(params.get("command") or params.get("content") or "")
        path = str(
            params.get("path")
            or params.get("target_path")
            or params.get("file_path")
            or params.get("target")
            or ""
        )

        category = self.classify_action_category(action, command=command, path=path)
        risk_level = self.risk_classifier.classify(
            action_type=action, path=path, content=command
        )

        is_override = self._is_elevated(channel, user_id, session_id)

        # Для CRITICAL включена 2-ступенчатая валидация независимо от Owner Override
        if category == ActionCategory.CRITICAL or risk_level == RiskLevel.CRITICAL:
            pending = self.create_2step_confirmation(
                channel=channel,
                user_id=user_id,
                session_id=session_id,
                command=command or action,
                path=path,
                tool_name=action,
                args=params,
            )
            return {
                "allowed": False,
                "requires_approval": True,
                "requires_2step_confirmation": True,
                "category": category.value,
                "risk_level": "CRITICAL",
                "reason": "CRITICAL-действие требует 2-ступенчатого целевого подтверждения.",
                "pending_confirmation": pending,
                "formatted_message": pending.format_prompt(),
            }

        # P1-001: HIGH-risk actions require approval (no Owner Override bypass).
        if risk_level == RiskLevel.HIGH:
            return {
                "allowed": False,
                "requires_approval": True,
                "requires_2step_confirmation": False,
                "category": category.value,
                "risk_level": "HIGH",
                "reason": "HIGH-risk action requires approval grant before execution.",
            }

        # Для SAFE и SENSITIVE: Разрешено.
        # P1-001: LOW is not forced through the HIGH-only approval gate.
        if category == ActionCategory.SAFE or risk_level == RiskLevel.LOW:
            requires_appr = False
            risk_label = "LOW"
        else:
            requires_appr = False if is_override else self.require_approval
            risk_label = "MEDIUM"
        if requires_appr:
            # The verdict ALLOWS the action but still demands an owner approval, so the
            # reason must say that - reporting "passed the security check" here made an
            # approval requirement look like a successful check.
            reason_str = (
                "Действие разрешено политикой, но требует одобрения владельца "
                "перед выполнением."
            )
        elif is_override:
            reason_str = (
                "Owner Override активен: сняты требования политики для SAFE/SENSITIVE."
            )
        else:
            reason_str = "Действие прошло проверку политики безопасности."

        return {
            "allowed": True,
            "requires_approval": requires_appr,
            "requires_2step_confirmation": False,
            "category": category.value,
            "risk_level": risk_label,
            "reason": reason_str,
        }

    async def check_shell(self, command: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Проверить shell-команду на безопасность."""
        return await self.check("RUN_SHELL", params={"command": command}, context=context)


async def confirm_pending_globally(
    channel: str, user_id: str | int, user_input: str
) -> dict[str, Any] | None:
    """Resolve ``/confirm <token>`` against any engine in this process.

    A surface (Telegram ``/confirm``) holds only the token, so it cannot reach
    the ``PolicyEngine`` instance that created the pending confirmation. This
    looks the token up in the process registry and delegates to the canonical
    APPROVAL → RE-DISPATCH bridge, which mints and consumes the one-shot grant.

    Returns ``None`` when no such pending confirmation exists here (the caller
    then falls back to its own fail-closed rejection); otherwise the verdict
    dict of :meth:`PolicyEngine.confirm_and_continue`.
    """
    token = _extract_candidate_token(user_input)
    if not token:
        return None
    engine = _GLOBAL_PENDING.get(token)
    if engine is None:
        return None
    pending = engine.pending_for_token(token)
    if pending is None:
        _GLOBAL_PENDING.pop(token, None)
        return None
    # The pending confirmation carries its own session binding; channel and
    # user identity still have to match, so a foreign surface cannot confirm.
    return await engine.confirm_and_continue(
        channel,
        user_id,
        pending.session_id,
        f"/confirm {token}",
        tool_name=pending.tool_name,
        args=pending.args,
    )
