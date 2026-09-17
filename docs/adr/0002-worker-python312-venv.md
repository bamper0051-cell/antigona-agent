# ADR-0002: Worker на отдельном venv Python 3.12

**Статус:** Accepted
**Дата:** 2026-07-25
**Решает:** развилку из ROADMAP.md (Шаг 2)

## Контекст
- Репозиторий Antigona требует Python 3.11+ (AGENTS.md, pyproject).
- OpenHands SDK (`openhands-sdk` 1.36.1) жёстко требует Python 3.12 (upstream issue #1363: «Currently we only support Python 3.12»).
- CLI upstream отстаёт (1.21.0 vs SDK 1.36.1) — не используем, свой клиент.

## Решение
- Создать выделенный venv **`.venv-worker312`** на `/usr/bin/python3.12` для процесса **Worker** (интеграция OpenHands SDK).
- Остальные процессы (Gateway, Verifier) остаются на основном `.venv` (Python 3.11).
- Зафиксировано в `docs/DECISIONS.md` (раздел Python-venv).

## Последствия
- ✅ Worker может грузить OpenHands SDK без понижения версии репо.
- ✅ Изоляция: падение/обновление SDK не ломает Gateway/Verifier.
- ⚠️ Два venv — нужно явно активировать правильный при запуске каждого сервиса (systemd-юниты указывают свой venv).
- ⚠️ `ruff`/`mypy` для worker-пакета запускать из `.venv-worker312` (или системного, где установлены); в worker-venv сами линтеры не ставились — прогон через основной venv показал чисто.

## Проверка (2026-07-25)
- `.venv-worker312/bin/python --version` → 3.12 ✅
- `pip show openhands-sdk` → 1.36.1 ✅
- `pytest tests/` (основной venv) → 58 passed, включая worker-интеграционные ✅
- Чекпоинт: flow «создай файл в workspace» → файл на диске (0750); рестарт Worker → разговор восстановлен из `persistence_dir` ✅
