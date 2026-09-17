# Карта интерфейса и backend-доступности — Этап 1

Это **инвентаризация**, а не дизайн/реализация экранов. Кнопки со статусом `NOT_IMPLEMENTED` не существуют в текущем Telegram-коде; `BACKEND_UNAVAILABLE` означает отсутствие необходимого Gateway API.

## Текущий фактический UI

```text
DeliveryOutbox transition
  └─ antigona-delivery
      └─ Telegram Bot API sendMessage
          └─ "<task_id>: <status> — <message>"
```

Навигации, меню, карточек, callback data, pagination и редактирования сообщений нет.

## Матрица требуемого Command Center

| Раздел/действие | Telegram UI | Backend | Реальное соответствие |
|---|---|---|---|
| Главная статус-карточка | NOT_IMPLEMENTED | Частично: `GET /health` | Только `status` и имя sandbox backend; очередь/workers/DB/model/memory metrics недоступны |
| Новая задача | NOT_IMPLEMENTED | Частично IMPLEMENTED: `POST /tasks` | Требует goal/path/content/tool; нет вложений, режима, бюджетов и предварительной карточки |
| Мои задачи — список/фильтры | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | Есть только `GET /tasks/{id}` |
| Карточка задачи — обзор/шаги/проверки | NOT_IMPLEMENTED | Частично IMPLEMENTED | `TaskView` содержит steps/transitions/artifacts/approvals, если известен ID |
| Запуск/повтор | NOT_IMPLEMENTED | IMPLEMENTED: `POST /tasks/{id}/run` | Sticky terminal tasks не возобновляются |
| Отмена | NOT_IMPLEMENTED | IMPLEMENTED: `POST /tasks/{id}/cancel` | Sticky cancellation реализована |
| Пауза/продолжение | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | Состояний PAUSED нет |
| Approval approve/reject | NOT_IMPLEMENTED | Частично IMPLEMENTED | Boolean endpoint; нет expiry/temporary permission/list |
| Артефакты | NOT_IMPLEMENTED | Частично в `TaskView` | Нет download/export endpoint |
| Цели/Task Flow catalog | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | `task_flows` — конкретные задачи, отдельной модели Goals нет |
| Агенты/подагенты/дебаты | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | Один тип worker loop; API управления нет |
| Инструменты | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | В коде два инструмента: `workspace.write_text`, `sandbox.shell`; catalog API нет |
| Память/skills | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | Нет моделей и API |
| Безопасность | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | Политики частично зашиты в код и systemd; read API нет |
| Мониторинг/логи | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | `/health` минимален; metrics/log API нет |
| Настройки | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | Только environment settings, API нет |
| О системе | NOT_IMPLEMENTED | Частично | FastAPI metadata version `0.2.0`; специального endpoint нет |
| Остановить всё | NOT_IMPLEMENTED | BACKEND_UNAVAILABLE | Global stop отсутствует |

## Фактические состояния, видимые по исходящим событиям

Outbox создаётся на каждом journal transition. Потенциальные значения status: `RECEIVED`, `QUEUED`, `PLANNING`, `WAITING_APPROVAL`, `TOOL_EXECUTING`, `OBSERVING`, `VERIFYING`, `DONE`, `FAILED`, `BLOCKED`, `CANCELLED`, `TIMEOUT`, `POLICY_DENIED`, а также step-состояния. Доставка — отдельные сообщения; single-message card/update не реализованы.

## Доступность требуемых UI-состояний

| UI state | Статус |
|---|---|
| loading / empty / stale / partial failure / permission denied | NOT_IMPLEMENTED |
| success | Только текст transition, без UI state model |
| unavailable / fatal error | Retry хранится в outbox (`last_error`), пользователю отдельная карточка не показывается |

## Граница следующего этапа

До проектирования навигации нужно сначала утвердить, какие отсутствующие backend contracts будут P0. Нельзя рисовать как рабочие: списки задач/approvals, pause/resume, global stop, metrics, logs, goals, agents, memory, skills и settings.
