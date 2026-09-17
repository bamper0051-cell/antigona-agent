"""Durable Execution Kernel (M1) — durable Task/Run execution.

Runtime truth = durable storage. LLM and worker processes are stateless
executors; tasks and their runs live in SQLite and survive crash/restart.
"""
from __future__ import annotations

from .dispatcher import KernelDispatcher
from .executor import KernelExecutor
from .models import (
    KernelDependency,
    KernelRun,
    KernelTask,
    KernelTransition,
)
from .state import (
    KernelStateError,
    RunState,
    TaskState,
)
from .store import (
    KernelClaimError,
    KernelFenceError,
    KernelNotFoundError,
    KernelStore,
)

__all__ = [
    "KernelTask",
    "KernelRun",
    "KernelDependency",
    "KernelTransition",
    "TaskState",
    "RunState",
    "KernelStateError",
    "KernelClaimError",
    "KernelFenceError",
    "KernelNotFoundError",
    "KernelStore",
    "KernelExecutor",
    "KernelDispatcher",
]
