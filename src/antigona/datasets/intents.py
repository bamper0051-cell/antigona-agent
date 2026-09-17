"""Balanced intent classification curriculum — aligned with IntentRouter output.

Each example has ``text``, ``expected_intent`` (matching the router's actual
classification), and ``tags``.  Coverage: 10+ intents, 5+ examples each.
"""

from __future__ import annotations

from typing import Any

# ── Intent examples (router-actual intent names) ───────────────────────────

intent_examples: list[dict[str, Any]] = [
    # ── Greeting ──────────────────────────────────────────────────────────
    {"text": "Привет", "expected_intent": "conversation.greeting", "tags": ["greeting", "ru"]},
    {"text": "Здравствуйте", "expected_intent": "conversation.greeting", "tags": ["greeting", "formal", "ru"]},
    {"text": "Хай", "expected_intent": "conversation.greeting", "tags": ["greeting", "ru"]},
    {"text": "Hello", "expected_intent": "conversation.greeting", "tags": ["greeting", "en"]},
    {"text": "Доброе утро", "expected_intent": "conversation.greeting", "tags": ["greeting", "ru"]},
    {"text": "Здорово", "expected_intent": "conversation.greeting", "tags": ["greeting", "ru"]},
    {"text": "Приветствую", "expected_intent": "conversation.greeting", "tags": ["greeting", "formal", "ru"]},

    # ── Identity ──────────────────────────────────────────────────────────
    {"text": "Кто ты?", "expected_intent": "conversation.identity", "tags": ["identity", "ru"]},
    {"text": "Ты кто?", "expected_intent": "conversation.identity", "tags": ["identity", "ru"]},
    {"text": "Кто такой?", "expected_intent": "conversation.identity", "tags": ["identity", "ru"]},
    {"text": "Что ты такое?", "expected_intent": "conversation.identity", "tags": ["identity", "ru"]},
    {"text": "Ты кто такой?", "expected_intent": "conversation.identity", "tags": ["identity", "ru"]},
    {"text": "Вы кто?", "expected_intent": "conversation.identity", "tags": ["identity", "formal", "ru"]},

    # ── Thanks ─────────────────────────────────────────────────────────────
    {"text": "Спасибо", "expected_intent": "conversation.thanks", "tags": ["thanks", "ru"]},
    {"text": "Спс", "expected_intent": "conversation.thanks", "tags": ["thanks", "short", "ru"]},
    {"text": "Пасибо", "expected_intent": "conversation.thanks", "tags": ["thanks", "ru"]},
    {"text": "Благодарю", "expected_intent": "conversation.thanks", "tags": ["thanks", "formal", "ru"]},
    {"text": "Thanks", "expected_intent": "conversation.thanks", "tags": ["thanks", "en"]},
    {"text": "Thank you", "expected_intent": "conversation.thanks", "tags": ["thanks", "en"]},
    {"text": "thx", "expected_intent": "conversation.thanks", "tags": ["thanks", "short", "en"]},

    # ── Goodbye ────────────────────────────────────────────────────────────
    {"text": "Пока", "expected_intent": "conversation.goodbye", "tags": ["goodbye", "ru"]},
    {"text": "До свидания", "expected_intent": "conversation.goodbye", "tags": ["goodbye", "formal", "ru"]},
    {"text": "bye", "expected_intent": "conversation.goodbye", "tags": ["goodbye", "en"]},
    {"text": "see you", "expected_intent": "conversation.goodbye", "tags": ["goodbye", "en"]},
    {"text": "goodbye", "expected_intent": "conversation.goodbye", "tags": ["goodbye", "en"]},
    {"text": "Чао", "expected_intent": "conversation.goodbye", "tags": ["goodbye", "ru"]},
    {"text": "Увидимся", "expected_intent": "conversation.goodbye", "tags": ["goodbye", "ru"]},

    # ── Noise ──────────────────────────────────────────────────────────────
    {"text": "?", "expected_intent": "conversation.noise", "tags": ["noise", "short"]},
    {"text": "!", "expected_intent": "conversation.noise", "tags": ["noise", "short"]},
    {"text": "…", "expected_intent": "conversation.noise", "tags": ["noise", "short"]},
    {"text": "???", "expected_intent": "conversation.noise", "tags": ["noise", "short"]},
    {"text": "а", "expected_intent": "conversation.noise", "tags": ["noise", "short", "ru"]},
    {"text": "😊", "expected_intent": "conversation.noise", "tags": ["noise", "emoji"]},
    {"text": "🤔", "expected_intent": "conversation.noise", "tags": ["noise", "emoji"]},
    {"text": "Hi", "expected_intent": "conversation.noise", "tags": ["greeting", "en", "short"]},

    # ── Smalltalk ──────────────────────────────────────────────────────────
    {"text": "is there a test suite?", "expected_intent": "conversation.smalltalk", "tags": ["smalltalk", "en", "question"]},
    {"text": "what's a policy engine?", "expected_intent": "conversation.smalltalk", "tags": ["smalltalk", "en", "question"]},
    {"text": "explain how the orchestrator works", "expected_intent": "conversation.smalltalk", "tags": ["smalltalk", "en", "question"]},
    {"text": "что такое жизнь", "expected_intent": "conversation.smalltalk", "tags": ["smalltalk", "ru", "question"]},
    {"text": "tell me more", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "vague", "en"]},
    {"text": "I like this", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "vague", "en"]},
    {"text": "cool", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "vague", "en"]},
    {"text": "do something", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "vague", "en"]},
    {"text": "fix everything", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "vague", "en"]},

    # ── Task: file write ───────────────────────────────────────────────────
    {"text": "создай файл test.txt", "expected_intent": "task.file_write", "tags": ["task", "file", "ru"]},
    {"text": "напиши файл hello.py с принтом", "expected_intent": "task.file_write", "tags": ["task", "file", "ru"]},
    {"text": "make file config.yaml", "expected_intent": "task.file_write", "tags": ["task", "file", "en"]},
    {"text": "write file README.md", "expected_intent": "task.file_write", "tags": ["task", "file", "en"]},
    {"text": "create file notes.txt", "expected_intent": "task.file_write", "tags": ["task", "file", "en"]},
    {"text": "создай файл в папке data data.csv", "expected_intent": "task.file_write", "tags": ["task", "file", "ru"]},
    {"text": "напиши файл /tmp/out.log", "expected_intent": "task.file_write", "tags": ["task", "file", "ru"]},

    # ── Task: shell ────────────────────────────────────────────────────────
    {"text": "запусти тесты", "expected_intent": "task.shell", "tags": ["task", "shell", "ru"]},
    {"text": "выполни npm install", "expected_intent": "task.shell", "tags": ["task", "shell", "ru"]},
    {"text": "shell: pip install pytest", "expected_intent": "task.shell", "tags": ["task", "shell", "prefix"]},
    {"text": "install packages", "expected_intent": "task.shell", "tags": ["task", "shell", "en"]},
    {"text": "установи зависимости", "expected_intent": "task.shell", "tags": ["task", "shell", "ru"]},
    {"text": "перезапусти сервис", "expected_intent": "task.shell", "tags": ["task", "shell", "ru"]},
    {"text": "fix typo in docs", "expected_intent": "task.shell", "tags": ["task", "shell", "en"]},

    # ── Task: code change ──────────────────────────────────────────────────
    {"text": "реализуй функцию parse_json", "expected_intent": "task.code_change", "tags": ["task", "code", "ru"]},
    {"text": "добавь функционал сортировки", "expected_intent": "task.code_change", "tags": ["task", "code", "ru"]},
    {"text": "implement feature search", "expected_intent": "task.code_change", "tags": ["task", "code", "en"]},
    {"text": "add feature user auth", "expected_intent": "task.code_change", "tags": ["task", "code", "en"]},
    {"text": "refactor main module", "expected_intent": "task.code_change", "tags": ["task", "code", "en"]},
    {"text": "сделай фичу логирования", "expected_intent": "task.code_change", "tags": ["task", "code", "ru"]},
    {"text": "реализуй парсер", "expected_intent": "task.code_change", "tags": ["task", "code", "ru"]},

    # ── Task: file edit ────────────────────────────────────────────────────
    {"text": "исправь ошибку в config.py", "expected_intent": "task.file_edit", "tags": ["task", "edit", "ru"]},
    {"text": "измени путь в settings", "expected_intent": "task.file_edit", "tags": ["task", "edit", "ru"]},
    {"text": "добавь строку в README", "expected_intent": "task.file_edit", "tags": ["task", "edit", "ru"]},
    {"text": "удали строку из файла", "expected_intent": "task.file_edit", "tags": ["task", "edit", "ru"]},
    {"text": "обнови версию в pyproject.toml", "expected_intent": "task.file_edit", "tags": ["task", "edit", "ru"]},
    {"text": "check the log file", "expected_intent": "task.shell", "tags": ["task", "shell", "en"]},
    {"text": "run tests", "expected_intent": "task.shell", "tags": ["task", "shell", "en"]},

    # ── Analysis: explain ──────────────────────────────────────────────────
    {"text": "объясни как работает интент роутер", "expected_intent": "analysis.explain", "tags": ["analysis", "explain", "ru"]},
    {"text": "расскажи о проекте", "expected_intent": "analysis.explain", "tags": ["analysis", "explain", "ru"]},
    {"text": "опиши архитектуру", "expected_intent": "analysis.explain", "tags": ["analysis", "explain", "ru"]},
    {"text": "почему так сделано", "expected_intent": "analysis.explain", "tags": ["analysis", "explain", "ru"]},
    {"text": "покажи статус", "expected_intent": "analysis.explain", "tags": ["analysis", "explain", "ru"]},
    {"text": "расскажи о Verifier", "expected_intent": "analysis.explain", "tags": ["analysis", "explain", "ru"]},
    {"text": "зачем это нужно?", "expected_intent": "analysis.explain", "tags": ["analysis", "explain", "ru"]},

    # ── Analysis: inspect (readonly) ──────────────────────────────────────
    {"text": "проверь файл лога", "expected_intent": "analysis.inspect_readonly", "tags": ["analysis", "inspect", "ru"]},
    {"text": "посмотри код main.py", "expected_intent": "analysis.inspect_readonly", "tags": ["analysis", "inspect", "ru"]},
    {"text": "найди ошибку в конфиге", "expected_intent": "analysis.inspect_readonly", "tags": ["analysis", "inspect", "ru"]},
    {"text": "проверь статус", "expected_intent": "analysis.inspect_readonly", "tags": ["analysis", "inspect", "ru"]},
    {"text": "read the config", "expected_intent": "analysis.inspect_readonly", "tags": ["analysis", "inspect", "en"]},

    # ── Question: project ──────────────────────────────────────────────────
    {"text": "можно ли запустить тесты?", "expected_intent": "question.project", "tags": ["question", "project", "ru"]},
    {"text": "как мне установить зависимости?", "expected_intent": "question.project", "tags": ["question", "project", "ru"]},
    {"text": "что такое IntentRouter?", "expected_intent": "question.project", "tags": ["question", "project", "ru"]},
    {"text": "что ты умеешь", "expected_intent": "question.general", "tags": ["question", "general", "ru"]},

    # ── Question: general ──────────────────────────────────────────────────
    {"text": "как дела?", "expected_intent": "question.general", "tags": ["question", "general", "ru"]},
    {"text": "что нового?", "expected_intent": "question.general", "tags": ["question", "general", "ru"]},
    {"text": "где файл?", "expected_intent": "question.general", "tags": ["question", "general", "ru"]},
    {"text": "сколько времени?", "expected_intent": "question.general", "tags": ["question", "general", "ru"]},
    {"text": "how are you?", "expected_intent": "question.general", "tags": ["question", "general", "en"]},
    {"text": "what's up?", "expected_intent": "question.general", "tags": ["question", "general", "en"]},

    # ── Mixed intents ──────────────────────────────────────────────────────
    {"text": "Who are you?", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "identity", "en"]},
    {"text": "update config.yaml", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "edit", "en"]},
    {"text": "show me how it works", "expected_intent": "ambiguous.mixed_intent", "tags": ["mixed", "question", "en"]},

    # ── Commands (only slash prefixes are detected) ────────────────────────
    {"text": "ping", "expected_intent": "command.status", "tags": ["command", "healthcheck", "en"]},
    {"text": "/status", "expected_intent": "command.status", "tags": ["command", "slash"]},
    {"text": "/skills", "expected_intent": "command.status", "tags": ["command", "slash"]},
    {"text": "/health", "expected_intent": "command.help", "tags": ["command", "slash"]},
    {"text": "/ping", "expected_intent": "command.help", "tags": ["command", "slash"]},
    {"text": "/start", "expected_intent": "command.start", "tags": ["command", "slash"]},
    {"text": "/help", "expected_intent": "command.help", "tags": ["command", "slash"]},
    {"text": "/quit", "expected_intent": "command.help", "tags": ["command", "slash"]},
    {"text": "/stop", "expected_intent": "command.help", "tags": ["command", "slash"]},
    {"text": "/restart", "expected_intent": "command.help", "tags": ["command", "slash"]},
    {"text": "/cancel", "expected_intent": "command.cancel", "tags": ["command", "slash"]},
    {"text": "/resume", "expected_intent": "command.resume", "tags": ["command", "slash"]},
    {"text": "/model", "expected_intent": "command.model_select", "tags": ["command", "model", "slash"]},
    {"text": "/setllm", "expected_intent": "command.model_select", "tags": ["command", "model", "slash"]},
    {"text": "/logs", "expected_intent": "command.help", "tags": ["command", "slash"]},
    {"text": "/debug", "expected_intent": "command.help", "tags": ["command", "slash"]},

    # ── Ambiguous followup ────────────────────────────────────────────────
    {"text": "продолжай", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "ru"]},
    {"text": "дальше", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "ru"]},
    {"text": "ещё", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "ru"]},
    {"text": "continue", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "en"]},
    {"text": "next", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "en"]},
    {"text": "more", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "en"]},
    {"text": "проверь", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "bare", "ru"]},
    {"text": "исправь", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "bare", "ru"]},
    {"text": "создай", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "bare", "ru"]},
    {"text": "сделай", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "bare", "ru"]},
    {"text": "напиши", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "bare", "ru"]},
    {"text": "запусти", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "bare", "ru"]},
    {"text": "удали", "expected_intent": "ambiguous.followup", "tags": ["ambiguous", "bare", "ru"]},
]


# ── Grouped by intent ──────────────────────────────────────────────────────

INTENT_CATEGORIES = [
    "conversation.greeting",
    "conversation.identity",
    "conversation.thanks",
    "conversation.goodbye",
    "conversation.noise",
    "conversation.smalltalk",
    "task.file_write",
    "task.shell",
    "task.code_change",
    "task.file_edit",
    "analysis.explain",
    "analysis.inspect_readonly",
    "question.project",
    "question.general",
    "ambiguous.mixed_intent",
    "command.status",
    "command.start",
    "command.help",
    "command.cancel",
    "command.resume",
    "ambiguous.followup",
]


def load() -> list[dict[str, Any]]:
    """Return all intent examples."""
    return intent_examples


def by_intent() -> dict[str, list[dict[str, Any]]]:
    """Group examples by expected_intent."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for ex in intent_examples:
        intent = ex["expected_intent"]
        groups.setdefault(intent, []).append(ex)
    return groups


def check_balance(min_per_intent: int = 5) -> dict[str, int | list[str]]:
    """Check balance — each intent category has at least min_per_intent examples."""
    groups = by_intent()
    under = [intent for intent, examples in groups.items() if len(examples) < min_per_intent]
    return {
        "total_examples": len(intent_examples),
        "unique_intents": len(groups),
        "under_minimum": under,
        "balanced": len(under) == 0,
    }
