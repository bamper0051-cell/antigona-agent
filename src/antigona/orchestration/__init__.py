"""Autonomous Goal Orchestration (M2).

OWNER → Gateway/Session → GOAL ENGINE → DURABLE FLOW → Planner → Task DAG
→ M1 Durable Execution Kernel → SERVICE ROUTER → services → results/events
→ Independent Verification → GOAL JUDGE → DONE/CONTINUE/WAIT/REPLAN/BLOCKED.

Runtime truth = durable storage (goals, goal_flows, wake_events,
service_health, service_handoffs on the shared Base). LLM workers are
stateless executors; the durable Goal/Flow survive restart and failover.
"""
from .engine import GoalEngine
from .executors import ExecResult, ServiceExecutors
from .judge import GoalJudge, JudgeVerdict
from .models import (
    Goal,
    GoalFlow,
    ServiceHandoff,
    ServiceHealthState,
    WakeEvent,
)
from .planner import GoalPlanner
from .router import ServiceRouter
from .state import (
    Capacity,
    FailureClass,
    FlowState,
    GoalState,
    JudgeDecision,
    ServiceState,
    WakeKind,
    WakeStatus,
)
from .store import (
    FlowConflictError,
    GoalTransitionError,
    OrchestrationError,
    OrchestrationStore,
)
from .wake import WakeManager

__all__ = [
    "Goal",
    "GoalFlow",
    "ServiceHandoff",
    "ServiceHealthState",
    "WakeEvent",
    "Capacity",
    "FailureClass",
    "FlowState",
    "GoalState",
    "JudgeDecision",
    "ServiceState",
    "WakeKind",
    "WakeStatus",
    "FlowConflictError",
    "GoalTransitionError",
    "OrchestrationError",
    "OrchestrationStore",
    "GoalEngine",
    "ExecResult",
    "ServiceExecutors",
    "GoalJudge",
    "JudgeVerdict",
    "GoalPlanner",
    "ServiceRouter",
    "WakeManager",
]
