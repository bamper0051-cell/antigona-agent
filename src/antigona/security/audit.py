"""SystemAuditLogger — Безопасный аудиторский журнал системных действий Antigona.

Обеспечивает:
- Запись каждого системного действия с (channel, user_id, session_id, command, exit_code, timestamp).
- Гарантированную маскировку сырых PIN, токенов, ключей и секретов в логах и БД.
- Хранение в SQLite БД с поддержкой выгрузки логов.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("antigona.audit")

# ─── Шаблоны для удаления секретов из логов ─────────────────────────────────────

# Регулярные выражения для поиска секретных данных
_PIN_PATTERNS = [
    (re.compile(r"(/pin\s+)(\S+)", re.IGNORECASE), r"\1[REDACTED_PIN]"),
    (re.compile(r"(\bpin\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_PIN]\3"),
    (re.compile(r"(\bANTIGONA_PIN\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_PIN]\3"),
]

_TOKEN_PATTERNS = [
    (re.compile(r"(/confirm\s+)(\S+)", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(\btoken\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_TOKEN]\3"),
    (re.compile(r"(\bBearer\s+)([a-zA-Z0-9._\-]+)", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"\b(sk-[a-zA-Z0-9_-]{20,})\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b(ghp_[a-zA-Z0-9]{20,})\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b(hf_[a-zA-Z0-9]{20,})\b"), "[REDACTED_TOKEN]"),
]

_SECRET_PATTERNS = [
    (re.compile(r"(\bpassword\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"(\bpasswd\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"(\bsecret\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"(\bapi_key\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"(\bprivate_key\s*[:=]\s*['\"]?)([^'\"\s&,]+)(['\"]?)", re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
]


def sanitize_secrets(value: Any) -> Any:
    """Очистить значение от сырых PIN-кодов, токенов и секретов."""
    if isinstance(value, str):
        sanitized = value
        for pattern, replacement in _PIN_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        for pattern, replacement in _TOKEN_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        for pattern, replacement in _SECRET_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    if isinstance(value, dict):
        cleaned_dict: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k).lower()
            if any(s in key_str for s in ("pin", "token", "password", "passwd", "secret", "key")):
                cleaned_dict[k] = "[REDACTED_SECRET]"
            else:
                cleaned_dict[k] = sanitize_secrets(v)
        return cleaned_dict

    if isinstance(value, list):
        return [sanitize_secrets(item) for item in value]

    return value


@dataclass
class AuditEntry:
    """Аудиторская запись о системном действии."""

    channel: str
    user_id: str
    session_id: str
    command: str
    exit_code: int
    timestamp: float
    status: str = "SUCCESS"
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["command"] = sanitize_secrets(self.command)
        d["details"] = sanitize_secrets(self.details or {})
        return d


class SystemAuditLogger:
    """Менеджер аудиторского журнала системных действий.

    Обеспечивает создание таблиц и безопасную запись каждого действия.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is not None:
            self._db_path = Path(db_path)
        else:
            # Governed runtime resolver: dedicated override (ANTIGONA_AUDIT_DB_PATH)
            # > ANTIGONA_STATE_ROOT/audit_log.db > fail-closed in an immutable
            # deployment > dev/test default.  Never the read-only code root.
            from antigona.core.paths import audit_log_db

            self._db_path = audit_log_db()

        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Инициализировать схему таблицы audit_log."""
        try:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS audit_log (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            channel TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            session_id TEXT NOT NULL,
                            command TEXT NOT NULL,
                            exit_code INTEGER NOT NULL,
                            timestamp REAL NOT NULL,
                            status TEXT NOT NULL DEFAULT 'SUCCESS',
                            details_json TEXT NOT NULL DEFAULT '{}'
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id, timestamp)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(channel, user_id, timestamp)"
                    )
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            # Fail closed: a read-only/unwritable audit store must NEVER be
            # silently relocated (that would quietly weaken the audit trail).
            # The governed resolver (core.paths.audit_log_db) already guarantees
            # a writable runtime root, or raises an actionable RuntimeError.
            msg = str(exc).lower()
            if "readonly" in msg or "read-only" in msg or "unable to open" in msg:
                raise RuntimeError(
                    f"audit log database {self._db_path} is not writable: {exc}. "
                    "Set ANTIGONA_STATE_ROOT (or ANTIGONA_AUDIT_DB_PATH) to a "
                    "writable directory outside the read-only code root."
                ) from exc
            raise

    def log_action(
        self,
        channel: str,
        user_id: str | int,
        session_id: str,
        command: str,
        exit_code: int,
        timestamp: float | None = None,
        status: str = "SUCCESS",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Записать системное действие в аудит-лог.

        Всегда санирует command и details перед сохранением.
        """
        ts = time.time() if timestamp is None else timestamp
        clean_channel = str(channel)
        clean_user_id = str(user_id)
        clean_session_id = str(session_id)
        clean_command = str(sanitize_secrets(command))
        clean_details = sanitize_secrets(details or {})

        details_json = json.dumps(clean_details, ensure_ascii=False)

        try:
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO audit_log (channel, user_id, session_id, command, exit_code, timestamp, status, details_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_channel,
                            clean_user_id,
                            clean_session_id,
                            clean_command,
                            exit_code,
                            ts,
                            status,
                            details_json,
                        ),
                    )
                    row_id = cursor.lastrowid
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            # Fail closed — never silently relocate the audit trail.
            msg = str(exc).lower()
            if "readonly" in msg or "read-only" in msg:
                raise RuntimeError(
                    f"audit log database {self._db_path} is not writable: {exc}. "
                    "Set ANTIGONA_STATE_ROOT (or ANTIGONA_AUDIT_DB_PATH) to a "
                    "writable directory outside the read-only code root."
                ) from exc
            raise

        logger.info(
            "AUDIT_LOG [#%s]: channel=%s user_id=%s session_id=%s command='%s' exit_code=%d status=%s",
            row_id,
            clean_channel,
            clean_user_id,
            clean_session_id,
            clean_command,
            exit_code,
            status,
        )

        return {
            "id": row_id,
            "channel": clean_channel,
            "user_id": clean_user_id,
            "session_id": clean_session_id,
            "command": clean_command,
            "exit_code": exit_code,
            "timestamp": ts,
            "status": status,
            "details": clean_details,
        }

    def get_logs(
        self,
        channel: str | None = None,
        user_id: str | int | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Получить аудиторские записи по фильтру."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []

        if channel is not None:
            query += " AND channel = ?"
            params.append(str(channel))
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(str(user_id))
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(str(session_id))

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        conn = self._get_connection()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()

        results: list[dict[str, Any]] = []
        for r in rows:
            details = {}
            if r["details_json"]:
                try:
                    details = json.loads(r["details_json"])
                except Exception:
                    details = {}
            results.append(
                {
                    "id": r["id"],
                    "channel": r["channel"],
                    "user_id": r["user_id"],
                    "session_id": r["session_id"],
                    "command": r["command"],
                    "exit_code": r["exit_code"],
                    "timestamp": r["timestamp"],
                    "status": r["status"],
                    "details": details,
                }
            )
        return results

    def clear(self) -> None:
        """Очистить аудит-лог (для тестов)."""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute("DELETE FROM audit_log")
        finally:
            conn.close()
