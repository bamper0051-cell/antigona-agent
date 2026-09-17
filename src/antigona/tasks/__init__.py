"""Пакет событийной системы Antigona.

Содержит модели, EventBus, TaskManager и RecoveryManager для отслеживания
жизненного цикла задач, публикации событий и восстановления после рестарта.

Компоненты:
    models — Task, TaskStep, TaskStatus, TaskEvent, TASK_EVENT_TYPES
    event_bus — EventBus (in-process async pub/sub)
    manager — TaskManager (CRUD + JSON persist + event publishing)
    recovery — RecoveryManager (восстановление задач после перезапуска)

Использование::

    from antigona.tasks import (
        Task, TaskStep, TaskStatus, TaskEvent,
        EventBus, TaskManager, RecoveryManager, TASK_EVENT_TYPES,
    )

    # EventBus — синглтон
    bus = EventBus.get_instance()

    # TaskManager принимает event_bus
    manager = TaskManager(event_bus=bus)
    await manager.start()

    # RecoveryManager для восстановления после рестарта
    recovery = RecoveryManager(task_manager=manager)
    results = await recovery.recover_all()

    task = await manager.create_task(...)
"""

from antigona.tasks.event_bus import EventBus, EventHandler
from antigona.tasks.manager import TaskManager
from antigona.tasks.models import (
    TASK_EVENT_TYPES,
    ScheduledAction,
    Task,
    TaskEvent,
    TaskStatus,
    TaskStep,
)
from antigona.tasks.recovery import RecoveryManager, RecoveryResult

__all__ = [
    # Модели
    "Task",
    "TaskStep",
    "TaskStatus",
    "TaskEvent",
    "ScheduledAction",
    "TASK_EVENT_TYPES",
    # EventBus
    "EventBus",
    "EventHandler",
    # TaskManager
    "TaskManager",
    # Recovery
    "RecoveryManager",
    "RecoveryResult",
]
