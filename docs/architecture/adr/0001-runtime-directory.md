# ADR-001: Runtime Directory

- **Статус:** ACCEPTED (2026-07-31)
- **Связанные:** ADR-007 (Paths)

## Решение
Antigona имеет **два role-specific каталога `.antigona`** — архитектурное
разделение, НЕ дублирование:
- **Owner-level** `/var/lib/antigona` — secrets/, vault/, skills/, plugins/, traces/, audit, cli_state.
- **Project-local** `/opt/antigona/.antigona` — personalities/, SOUL.md, AGENTS.md, gateway_config.json.
- **Project root** `/opt/antigona` — процессы, БД, .env, workspace, .tasks, .memory.

## Причины
Конституция (`/opt/antigona/.antigona/AGENTS.md` §6) закрепляет разделение
owner-level (настройки/секреты владельца) и project-local (данные агента).
Forensic-аудит Rev2 §6 подтвердил: разные модули (secrets/vault/plugins vs
soul/tool_gateway) и разные процессы используют разные каталоги.

## Рассмотренные альтернативы
1. **Объединение в один каталог** — отклонено: смешает секреты владельца и
   данные агента, нарушит переносимость и §6 Конституции.
2. **Только `~/.antigona`** — отклонено: потеряет project-local изоляцию.
3. **Разделение (выбрано)** — две зоны ответственности.

## Последствия
- Пути резолвятся через `core/paths.py` (ADR-007): `owner_dir()` и
  `project_local_dir()`.
- Owner-level доступен по сети процессов, project-local — привязан к проекту.

## Условия изменения
Только по решению Owner; требует нового forensic-обоснования и миграции с
архив-first и откатом.
