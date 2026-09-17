# ADR-002: Configuration

- **Статус:** ACCEPTED (2026-07-31)

## Решение
Официальный runtime-конфиг Antigona — **env `ANTIGONA_*`** (из
`/opt/antigona/.env`, загружается run.sh с `export`). `config.py`
(Settings) читает только env (config.py:99-185).

Категории источников:
| Источник | Роль |
|---|---|
| env `ANTIGONA_*` | **Primary** (SSOT) |
| `/opt/antigona/.env` | **Primary (file)** — носитель env |
| `/opt/antigona/.antigona/gateway_config.json` | **Project-level (ACTIVE)** |
| `/var/lib/antigona/config.yaml` | **Legacy** (не читается ничем) |
| `/var/lib/antigona/pin.json` | **Legacy/DORMANT** (только antigona-cli) |

## Причины
Forensic-аудит Rev2 §2: config.yaml не читается ни одним модулем (grep по
загрузчикам пуст); живые процессы имеют env `ANTIGONA_*`. config.yaml
санитизирован (значения заменены на `${ENV}`).

## Рассмотренные альтернативы
1. **config.yaml как источник** — отклонено: мёртвый источник, требует
   загрузчика и рассинхрона с env.
2. **env как SSOT (выбрано)** — уже фактически так работает.

## Последствия
- Новые настройки добавляются через env + paths.py; config.yaml НЕ трогать.
- LEGACY-файлы не удаляются автоматически (см. ADR-005).

## Условия изменения
Введение YAML-конфига — только с единым загрузчиком и миграцией; требует
approval Owner и пересмотра validator.
