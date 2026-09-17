"""LongTermMemory — persistent facts, preferences, and style memory via SQLite.

Stores structured data across sessions so the bot remembers user identity,
preferences, and stylistic choices even after restart.

Tables:
    facts       — key/value/category store for user-specific facts
    preferences — key/value store for user preferences and style settings

Usage::

    memory = LongTermMemory(db_path=str(memory_db_path()))  # antigona.core.paths.memory_db_path()
    memory.save_fact("user_name", "Владелец", category="user")
    print(memory.get_fact("user_name"))                 # → "Владелец"
    block = memory.get_preferences_block()              # formatted for system prompt
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ─── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facts (
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (key, category)
);

CREATE TABLE IF NOT EXISTS preferences (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

WAL_PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
"""

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "..",
    "antigona_memory.db",
)


def _resolve_db_path(path: str | None) -> str:
    """Resolve DB path, defaulting to the governed ``paths.memory_db_path()``.

    The legacy default sat *beside the package root* (i.e. under the code
    root); in a hardened deployment that is a read-only tree, so resolution is
    delegated to the runtime state root (fail closed when unset).
    """
    if path:
        return path
    from antigona.core import paths

    return str(paths.memory_db_path())


# ─── LongTermMemory ────────────────────────────────────────────────────────────


class LongTermMemory:
    """Persistent long-term memory backed by SQLite.

    Thread-safe (single-connection, uses ``check_same_thread=False`` for
    lightweight usage). Not designed for high concurrency — one instance per
    process is the intended use pattern.

    Attributes:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path: str = _resolve_db_path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._connect()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def _connect(self) -> None:
        """Open or create the database with the schema applied."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        for pragma in WAL_PRAGMAS.strip().split(";"):
            pragma = pragma.strip()
            if pragma:
                self._conn.execute(pragma)
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        logger.info("LongTermMemory DB opened: %s", self.db_path)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> LongTermMemory:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ── Facts ───────────────────────────────────────────────────────────────

    def save_fact(self, key: str, value: str, category: str = "user") -> None:
        """Save a fact (upsert by key+category).

        Args:
            key: Fact key (e.g. ``"user_name"``).
            value: Fact value (e.g. ``"Владелец"``).
            category: Fact category (e.g. ``"user"``, ``"project"``).
        """
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO facts (key, value, category, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key, category) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (key, value, category, now, now),
        )
        self._conn.commit()

    def save_facts_bulk(self, facts: list[dict[str, str]]) -> int:
        """Save multiple facts in a single transaction.

        Each dict must have ``key`` and ``value``; ``category`` defaults to ``"user"``.

        Returns:
            Number of facts saved.
        """
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        count = 0
        for fact in facts:
            key = fact.get("key", "").strip()
            value = fact.get("value", "").strip()
            category = fact.get("category", "user").strip()
            if not key or not value:
                continue
            self._conn.execute(
                "INSERT INTO facts (key, value, category, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key, category) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, value, category, now, now),
            )
            count += 1
        self._conn.commit()
        return count

    def get_fact(self, key: str, category: str = "user") -> str:
        """Get a fact value by key and category.

        Returns:
            The fact value, or ``""`` if not found.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT value FROM facts WHERE key = ? AND category = ?",
            (key, category),
        )
        row = cur.fetchone()
        return str(row["value"]) if row else ""

    def get_all_facts(self) -> dict[str, dict[str, str]]:
        """Return all facts grouped by category.

        Returns::

            {
                "user": {"user_name": "Владелец"},
                "project": {"project_name": "Antigona"},
            }
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT key, value, category FROM facts ORDER BY category, key"
        )
        result: dict[str, dict[str, str]] = {}
        for row in cur.fetchall():
            cat = str(row["category"])
            if cat not in result:
                result[cat] = {}
            result[cat][str(row["key"])] = str(row["value"])
        return result

    def delete_fact(self, key: str, category: str = "user") -> bool:
        """Delete a fact by key and category.

        Returns:
            True if a row was deleted, False otherwise.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "DELETE FROM facts WHERE key = ? AND category = ?",
            (key, category),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── Preferences ─────────────────────────────────────────────────────────

    def save_preference(self, key: str, value: str) -> None:
        """Save a preference/style setting (upsert by key).

        Args:
            key: Preference key (e.g. ``"style_emojis"``).
            value: Preference value (e.g. ``"true"``).
        """
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )
        self._conn.commit()

    def save_preferences_bulk(self, prefs: dict[str, str]) -> int:
        """Save multiple preferences in a single transaction.

        Returns:
            Number of preferences saved.
        """
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        count = 0
        for key, value in prefs.items():
            key = key.strip()
            value = value.strip()
            if not key or not value:
                continue
            self._conn.execute(
                "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )
            count += 1
        self._conn.commit()
        return count

    def get_preference(self, key: str) -> str:
        """Get a preference value by key.

        Returns:
            The preference value, or ``""`` if not found.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT value FROM preferences WHERE key = ?",
            (key,),
        )
        row = cur.fetchone()
        return str(row["value"]) if row else ""

    def get_all_preferences(self) -> dict[str, str]:
        """Return all preferences as a flat dict."""
        assert self._conn is not None
        cur = self._conn.execute("SELECT key, value FROM preferences ORDER BY key")
        return {str(row["key"]): str(row["value"]) for row in cur.fetchall()}

    def delete_preference(self, key: str) -> bool:
        """Delete a preference by key.

        Returns:
            True if a row was deleted, False otherwise.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "DELETE FROM preferences WHERE key = ?",
            (key,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── Formatted blocks ────────────────────────────────────────────────────

    def get_preferences_block(self) -> str:
        """Build a formatted text block for injection into the system prompt.

        Returns something like::

            --- ФАКТЫ О ПОЛЬЗОВАТЕЛЕ ---
            - Пользователя зовут Владелец
            - Владелец — создатель Antigona

            --- ПРЕДПОЧТЕНИЯ СТИЛЯ ---
            - Использовать эмодзи в ответах
        """
        parts: list[str] = []
        facts = self.get_all_facts()

        # Build facts block
        if facts:
            parts.append("--- ФАКТЫ О ПОЛЬЗОВАТЕЛЕ ---")
            for _cat, cat_facts in facts.items():
                for key, value in cat_facts.items():
                    # Human-readable key mapping
                    label = _key_to_label(key)
                    parts.append(f"- {label}: {value}")

        prefs = self.get_all_preferences()
        style_lines: list[str] = []
        if prefs:
            for key, value in prefs.items():
                if value.lower() in ("true", "yes", "1"):
                    line = _pref_to_line(key)
                    if line:
                        style_lines.append(f"- {line}")

        if style_lines:
            if parts:
                parts.append("")
            parts.append("--- ПРЕДПОЧТЕНИЯ СТИЛЯ ---")
            parts.extend(style_lines)

        return "\n".join(parts)

    def has_memory(self) -> bool:
        """Check if there are any stored facts or preferences."""
        assert self._conn is not None
        cur = self._conn.execute("SELECT COUNT(*) FROM facts")
        fact_count = cur.fetchone()[0]
        cur = self._conn.execute("SELECT COUNT(*) FROM preferences")
        pref_count = cur.fetchone()[0]
        return bool(fact_count > 0) or bool(pref_count > 0)


# ─── Extraction helpers ────────────────────────────────────────────────────────


def _key_to_label(key: str) -> str:
    """Convert a fact key to a human-readable Russian label."""
    mapping: dict[str, str] = {
        "user_name": "Имя пользователя",
        "user_role": "Роль",
        "project_name": "Название проекта",
        "project_description": "Описание проекта",
        "language": "Язык общения",
        "topic": "Тема обсуждения",
    }
    if key in mapping:
        return mapping[key]
    # Fallback: capitalise and replace underscores
    return key.replace("_", " ").capitalize()


def _pref_to_line(key: str) -> str:
    """Convert a preference key to a human-readable Russian instruction."""
    mapping: dict[str, str] = {
        "style_emojis": "Использовать эмодзи в ответах",
        "style_swear": "Использовать разговорный стиль",
        "style_brief": "Писать кратко и по делу",
        "style_formal": "Писать в формальном стиле",
        "response_style": "",
        "preferred_model": "",
    }
    if key in mapping:
        return mapping[key]
    # Fallback
    return key.replace("_", " ").capitalize()


def parse_extracted_facts(text: str) -> dict[str, Any]:
    """Parse LLM output from fact extraction into structured data.

    Expected format::

        FACT|key|value|category
        PREFERENCE|key|value
        NO_FACTS

    Args:
        text: Raw LLM response.

    Returns:
        Dict with keys ``facts`` (list of dicts) and ``preferences`` (dict),
        or ``{"no_facts": True}`` if the LLM indicated nothing new.
    """
    result: dict[str, Any] = {
        "facts": [],
        "preferences": {},
    }

    stripped = text.strip()
    if not stripped or stripped.upper() == "NO_FACTS":
        result["no_facts"] = True
        return result

    for line in stripped.split("\n"):
        line = line.strip()
        if line.upper() == "NO_FACTS":
            result["no_facts"] = True
            continue

        parts = line.split("|")
        if len(parts) < 3:
            continue

        prefix = parts[0].strip().upper()
        if prefix == "FACT" and len(parts) >= 4:
            result["facts"].append({
                "key": parts[1].strip(),
                "value": parts[2].strip(),
                "category": parts[3].strip(),
            })
        elif prefix == "PREFERENCE" and len(parts) >= 3:
            result["preferences"][parts[1].strip()] = parts[2].strip()

    return result


# ─── Extraction prompt ─────────────────────────────────────────────────────────

EXTRACTION_PROMPT = (
    "Извлеки факты из диалога: имя пользователя, его проекты, "
    "предпочтения по стилю общения. "
    "Если ничего нового — ответь только 'NO_FACTS'. "
    "Формат:\n"
    "FACT|ключ|значение|категория\n"
    "PREFERENCE|ключ|значение\n\n"
    "Примеры:\n"
    "FACT|user_name|Владелец|user\n"
    "FACT|project_name|Antigona|project\n"
    "PREFERENCE|style_emojis|true\n"
    "PREFERENCE|response_style|emoji-friendly\n\n"
    "Извлеки ТОЛЬКО то, что явно сказано в диалоге. "
    "Если нет ничего нового — ответь NO_FACTS."
)
