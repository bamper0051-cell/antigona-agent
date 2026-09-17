# ADR-0001: Зафиксировать фактическую границу Telegram P0

- Статус: Accepted for Stage 1 audit
- Дата: 2026-07-22

## Контекст

Продуктовые требования описывают Telegram Command Center и отдельный поток Telegram Adapter → Gateway. Текущий код содержит отдельный Gateway, worker, verifier и send-only `TelegramAdapter`, но не содержит Telegram update consumer, handlers, callbacks, FSM или Gateway client для входящих событий. В `docs/ARCHITECTURE.md` фраза «Gateway remains the sole channel/polling owner» может ошибочно восприниматься как наличие poller.

## Решение

1. Считать текущий Telegram-контур **только исходящей durable progress delivery**.
2. Не считать Gateway Telegram adapter/poller реализованным до появления отдельного проверяемого inbound adapter.
3. Все Command Center элементы, которым нет endpoint, маркировать `BACKEND_UNAVAILABLE`; UI-код отсутствует — `NOT_IMPLEMENTED`.
4. Не добавлять декоративные handlers/buttons на Этапе 1.
5. Сохранить требуемую границу: будущий Telegram adapter не вызывает LLM, shell или tools напрямую, а использует Gateway.

## Последствия

- Проект не заявляет готовность Telegram Command Center.
- Следующий этап должен сначала согласовать недостающие backend contracts и inbound ownership.
- Send-only delivery может развиваться независимо, но для неё нужен отдельный deployment unit и тест live-конфигурации до production.
