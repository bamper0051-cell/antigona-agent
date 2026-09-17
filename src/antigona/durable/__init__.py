"""Durable state-machine primitives and operation progress tracking.

Single source of truth for the task/step transition graph, the guarded
transition checks, the append-only rejection audit, the lease-recovery
worker, and the operation progress lifecycle with progress messages.
"""

from __future__ import annotations

from antigona.durable.agent_loop import AutonomousLoop, LoopResult
from antigona.durable.execution_models import (
    AcceptanceCriterion,
    ExecutionPlan,
    Observation,
    PlanStep,
    ToolExecutionResult,
    VerificationVerdict,
)
from antigona.durable.operation_models import Operation, OperationState, StateMachine
from antigona.durable.operation_store import OperationStore

from .observer import Observer
from .planner import Planner
from .recovery import RecoveryReport, RecoveryWorker
from .state_machine import (
    STEP_TRANSITIONS,
    TASK_TRANSITIONS,
    TERMINAL_STATES,
    VERIFIER_ONLY_TRANSITIONS,
    ConcurrentUpdate,
    InvalidTransition,
    check_step_transition,
    check_task_transition,
    record_rejection,
)
from .verifier import Verifier

__all__ = [
    "AcceptanceCriterion",
    "AutonomousLoop",
    "ExecutionPlan",
    "LoopResult",
    "Observation",
    "Observer",
    "Operation",
    "OperationState",
    "OperationStore",
    "PlanStep",
    "Planner",
    "StateMachine",
    "STEP_TRANSITIONS",
    "TASK_TRANSITIONS",
    "TERMINAL_STATES",
    "VERIFIER_ONLY_TRANSITIONS",
    "ConcurrentUpdate",
    "InvalidTransition",
    "RecoveryReport",
    "RecoveryWorker",
    "ToolExecutionResult",
    "Verifier",
    "VerificationVerdict",
    "check_step_transition",
    "check_task_transition",
    "record_rejection",
]
