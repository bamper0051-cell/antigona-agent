# ADR-006: Secrets

- **Статус:** ACCEPTED (2026-07-31)

## Решение
Источники секретов Antigona (SSOT):
- **Telegram Bot Token** — env `TELEGRAM_BOT_TOKEN` (из `.env`).
- **PIN** — env `ANTIGONA_PIN` (из `.env`).
- **Owner Identity** — env `ANTIGONA_OWNER_ID`.
- **Gateway token / Verifier credential / dev token** — env +
  `/var/lib/antigona/secrets/*.json` (owner-level, читаются run.sh:30-39).
- **API-ключи LLM** — env (`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`).

Legacy/DORMANT источники секретов:
- `/var/lib/antigona/pin.json` — scrypt-хэш PIN для antigona-cli (спит).
- `/var/lib/antigona/vault/vault.db` + `master.key` — vault antigona-cli.
- `~/.hermes/secrets/antigona_pin.json` — legacy-fallback antigona-cli.

## Причины
Forensic-аудит Rev2 §3: живой runtime читает секреты из env + owner-level
secrets. pin.json/vault — только antigona-cli (0 процессов).

## Рассмотренные альтернативы
1. pin.json как SSOT PIN — отклонено: не читается ботом/gateway (env).
2. env + owner-level secrets (выбрано) — фактический runtime.

## Последствия
- Секреты НЕ хранятся в git; `.env` в .gitignore; значения не печатаются в
  отчёты.
- PIN единый (env) после консолидации antigona-cli.

## Условия изменения
Консолидация antigona-cli на env PIN — отдельный approval; до этого
pin.json/vault не трогать.
