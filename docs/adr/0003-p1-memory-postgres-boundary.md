# ADR-0003: Выделенный PostgreSQL/pgvector для памяти P1

- **Дата:** 2026-07-25
- **Статус:** Accepted

## Контекст

Первый последовательный шаг P1 требует векторную и profile-память на pgvector. ROADMAP относит полный перенос durable-state SQLite → PostgreSQL к P4 и разрешает раннюю миграцию только при фактических триггерах: конкуренция нескольких Worker, потребность в `SKIP LOCKED` или блокировки SQLite. Эти триггеры для durable-state не доказаны.

## Решение

- Поднять PostgreSQL с расширением pgvector только как хранилище памяти.
- Оставить task state, очередь и outbox в SQLite до триггеров P4.
- Хранить 1536-мерные embeddings, передаваемые вызывающим embedding-компонентом; memory layer не выбирает модель и не делает скрытых сетевых вызовов.
- Изолировать записи по `owner_id`; profile-факты обновлять по стабильному ключу `(owner_id, profile_key)`.
- Выполнять идемпотентную SQL-миграцию memory schema до использования store.

## Последствия

- P1 получает настоящий pgvector similarity search без преждевременного переписывания state machine/queue/outbox.
- В deployment появляются отдельный PostgreSQL DSN и обязанность выполнить `PostgresMemoryStore.migrate()`.
- Генерация embeddings остаётся отдельной зависимостью следующей интеграции с агентом; тесты memory store используют реальные PostgreSQL + pgvector, а не имитацию backend.
- Полная миграция durable-state остаётся P4 либо начнётся раньше только после измеримого trigger.
