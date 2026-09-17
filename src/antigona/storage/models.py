"""ORM surface of the async storage layer.

There is deliberately **no second set of models**: the async layer reuses the
declarative ``Base`` and every mapped class from :mod:`antigona.models`, so the
sync layer (:mod:`antigona.database`) and the async layer can never drift into
two different schemas. Alembic autogenerates against this same metadata.
"""

from __future__ import annotations

from ..database import SCHEMA_VERSION, Base
from ..models import (
    Approval,
    Artifact,
    CronSchedule,
    DeliveryOutbox,
    DurableOperation,
    FlowStep,
    QueueJob,
    ScheduleEvent,
    Skill,
    SkillTransition,
    StateTransition,
    StepState,
    TaskFlow,
    TaskState,
    utcnow,
)

#: Single metadata object shared by ``create_all`` (SQLite) and Alembic (Postgres).
metadata = Base.metadata

__all__ = [
    "SCHEMA_VERSION",
    "Approval",
    "Artifact",
    "Base",
    "CronSchedule",
    "DeliveryOutbox",
    "DurableOperation",
    "FlowStep",
    "QueueJob",
    "ScheduleEvent",
    "Skill",
    "SkillTransition",
    "StateTransition",
    "StepState",
    "TaskFlow",
    "TaskState",
    "metadata",
    "utcnow",
]
