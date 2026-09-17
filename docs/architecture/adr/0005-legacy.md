# ADR-005: Legacy

- **Статус:** ACCEPTED (2026-07-31)

## Решение
Компоненты, не читаемые живым runtime, признаются LEGACY. **Не удаляются
автоматически** — только archive-first после отдельного approval.

Реестр legacy (обоснование — forensic-аудит Rev2):
| Артефакт | Статус | Читается |
|---|---|---|
| `/var/lib/antigona/config.yaml` | **Legacy** | ничем |
| `/var/lib/antigona/db.sqlite` | **Legacy** | ничем |
| `/var/lib/antigona/personalities/` (пуст) | **Legacy** | ничем |
| `/var/lib/antigona/pin.json` | **DORMANT→Legacy** | только antigona-cli (спит) |
| `/var/lib/antigona/vault.db` + master.key | **DORMANT** | antigona-cli |
| `deploy/systemd/*` | **Legacy-шаблоны** | не установлен |
| `start_antigona_stack.sh` | **Legacy** | (gateway fallback — мёртв) |
| `/opt/antigona` | **DORMANT legacy-проект** | 0 процессов |
| `/opt/antigona.db` (в /root) | **Legacy/orphan** | ничем |

## Причины
grep-проверка загрузчиков (Rev2 §2/§4): config.yaml, db.sqlite не читаются
никаким кодом. pin.json/vault — источники спящего antigona-cli.

## Рассмотренные альтернативы
1. Автоматическое удаление — отклонено (риск, archive-first обязателен).
2. Реестр + документированный план миграции (выбрано) — см. LEGACY_MIGRATION_PLAN.

## Последствия
- CI guard (ADR-007) не даёт расти долгу (baseline обязан сокращаться).
- Удаление каждого legacy-элемента — отдельный approval + backup + откат.

## Условия изменения
pin.json/vault НЕ удалять до консолидации antigona-cli на env PIN (высокий
риск). Остальное — после подтверждения неиспользования и архива.
