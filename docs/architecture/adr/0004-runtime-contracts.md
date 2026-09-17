# ADR-004: Runtime Contracts

- **Статус:** ACCEPTED (2026-07-31)

## Решение
Явные архитектурные инварианты Antigona, проверяемые `startup/validator.py`
при каждом запуске (run.sh pre/post):
- C1: ровно 1 Telegram Bot
- C2: ровно 1 Gateway
- C3: ровно 1 Verifier
- C4: ровно 1 Worker
- C5: ровно 1 Delivery Worker
- C6: единая runtime DB (`/opt/antigona/antigona.db` + sessions)
- C7: единый Owner Identity (env `ANTIGONA_OWNER_ID`)
- C8: единый PIN (env `ANTIGONA_PIN`)
- C9: единый Bot Token (env `TELEGRAM_BOT_TOKEN`)
- C10: единый Runtime Provenance Chain (все процессы cwd=`/opt/antigona`)

## Причины
Дубли служб -> 409 Conflict Telegram / расщепление задач / гонки (Rev2 §D).
Частичный env (start_bot.sh) — источник расхождений. Зависший бот — из-за
незакрытого aiosqlite-потока (CONFLICT E, исправлен).

## Рассмотренные альтернативы
1. Нет валидатора (статус-кво) — отклонено: дубли/сироты не детектятся.
2. Валидатор (выбрано) — pre (нет устаревших процессов, жёсткий гейт) +
   post (полная проверка контрактов).

## Последствия
- CRITICAL-нарушение -> запуск отменяется (pre) с детальным отчётом.
- WARN (сироты) -> лог, запуск продолжается.
- Аварийный обход: `ANTIGONA_SKIP_VALIDATOR=1`.

## Условия изменения
Изменение любого инварианта (напр., разрешение 2 bot) — только с новым ADR
и пересмотром validator.
