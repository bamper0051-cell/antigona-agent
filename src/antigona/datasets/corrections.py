"""Error-driven learning — user corrections mapped to regression cases.

Each example has:
- ``user_text``: What the user said
- ``original_response``: How the system responded (may be wrong)
- ``correction``: The user's correction
- ``expected_intent``: The correct intent that should have been identified
- ``tags``: Categorization tags
"""

from __future__ import annotations

from typing import Any

correction_examples: list[dict[str, Any]] = [
    # ── Intent misclassification corrections ─────────────────────────────
    {
        "user_text": "Сделай файл",
        "original_response": "Что именно сделать?",
        "correction": "Создай файл test.txt",
        "expected_intent": "task.file_write",
        "tags": ["correction", "intent", "ru"],
    },
    {
        "user_text": "Проверь",
        "original_response": "Что проверить? Нужен объект.",
        "correction": "Проверь файл config.yaml на ошибки",
        "expected_intent": "analysis.inspect",
        "tags": ["correction", "intent", "ru"],
    },
    {
        "user_text": "Запусти",
        "original_response": "Запустить что?",
        "correction": "Запусти тесты pytest",
        "expected_intent": "task.shell",
        "tags": ["correction", "intent", "ru"],
    },

    # ── Clarification corrections ────────────────────────────────────────
    {
        "user_text": "Сделай что нужно",
        "original_response": "Уточните, что именно сделать.",
        "correction": "Установи зависимости из requirements.txt",
        "expected_intent": "task.shell",
        "tags": ["correction", "clarification", "ru"],
    },
    {
        "user_text": "Разберись",
        "original_response": "Не понял. Сформулируйте задачу.",
        "correction": "Проверь есть ли файлы в папке data/",
        "expected_intent": "analysis.inspect",
        "tags": ["correction", "clarification", "ru"],
    },

    # ── Safety / blocking corrections ────────────────────────────────────
    {
        "user_text": "Удали всё с сервера",
        "original_response": "Команда отклонена политикой безопасности.",
        "correction": "Нет, просто очисти кэш в папке /tmp/cache",
        "expected_intent": "task.shell",
        "tags": ["correction", "safety", "ru"],
    },

    # ── Scope corrections ────────────────────────────────────────────────
    {
        "user_text": "создай файл",
        "original_response": "Какой файл? Укажите имя и путь.",
        "correction": "создай файл src/antigona/new_module.py",
        "expected_intent": "task.file_write",
        "tags": ["correction", "scope", "ru"],
    },
    {
        "user_text": "исправь ошибку",
        "original_response": "Какую ошибку? Где?",
        "correction": "В файле main.py исправь импорт на from pathlib import Path",
        "expected_intent": "task.file_edit",
        "tags": ["correction", "scope", "ru"],
    },

    # ── Follow-up corrections (bare verbs resolved from context) ─────────
    {
        "user_text": "Создай файл /etc/config",
        "original_response": "Создаю файл /etc/config",
        "correction": "Нет, создай в /srv/config, не в /etc",
        "expected_intent": "task.file_write",
        "tags": ["correction", "followup", "path", "ru"],
    },
    {
        "user_text": "Напиши тест для функции parse",
        "original_response": "Пишу тест...",
        "correction": "Не пиши тест, просто проверь текущие тесты",
        "expected_intent": "task.shell",
        "tags": ["correction", "followup", "intent_switch", "ru"],
    },
]


def load() -> list[dict[str, Any]]:
    """Return all correction examples."""
    return correction_examples


def by_tag(tag: str) -> list[dict[str, Any]]:
    """Filter examples by a specific tag."""
    return [ex for ex in correction_examples if tag in ex.get("tags", [])]


def count_by_tag() -> dict[str, int]:
    """Return counts per tag category."""
    counts: dict[str, int] = {}
    for ex in correction_examples:
        for tag in ex.get("tags", []):
            parts = tag.split(".")
            for part in parts:
                counts[part] = counts.get(part, 0) + 1
    return counts
