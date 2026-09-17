# ADR-001: PostgreSQL TaskFlow — единый источник истины

**Статус:** ACCEPTED
**Дата:** 2026-07-28
**Контекст:** Проект имеет три параллельных контура управления задачами:
- Gateway → PostgreSQL TaskFlow (durable, CAS, Verifier)
- IntentRouter → TaskManager → JSON persist (новый, ставит DONE без Verifier)
- Dashboard → собственный TaskManager + EventBus (изолированный)

**Решение:** Все операции создания/изменения/завершения задач проходят через Gateway.
TaskManager и TaskRuntime сохраняются только как read-only адаптеры для старых JSON задач.
Dashboard переводится на Gateway client.
Verifier — единственный субъект, имеющий право переводить задачу в DONE.

**Последствия:**
- Одна модель задачи (PostgreSQL) вместо трёх
- Все интерфейсы (Telegram, CLI, Dashboard) проходят через единый auth/approval/sandbox pipeline
- TaskManager.complete_task() отключается для production
- TaskRuntime.subprocess(shell=True) заменяется на SandboxRunner
- Dashboard теряет возможность ставить DONE
- Необходима миграция существующих JSON-задач в PostgreSQL
