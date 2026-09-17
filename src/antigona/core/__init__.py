"""Antigona Core — единый application boundary."""

from antigona.core.consistency import ConsistencyReport, verify_task_consistency
from antigona.core.control_plane import (
    AntigonaControlPlane,
    ApprovalListEntry,
    ApprovalListView,
    ApprovalView,
    FlowStatus,
    FlowView,
    IntentClass,
    NormalizedRequest,
    SteeringCommand,
)
from antigona.core.event_log import EventLog, EventRecord
from antigona.core.evidence_registry import EvidenceRecord, EvidenceRegistry
from antigona.core.gateway_client import GatewayClient
from antigona.core.owner_gate import GateDecision, OwnerGate, OwnerGateError, approve_and_continue
from antigona.core.task_registry import TaskListFilter, TaskRecord, TaskRegistry

__all__ = [
    "AntigonaControlPlane",
    "ApprovalListEntry",
    "ApprovalListView",
    "ApprovalView",
    "ConsistencyReport",
    "EvidenceRecord",
    "EvidenceRegistry",
    "FlowStatus",
    "FlowView",
    "GateDecision",
    "EventLog",
    "EventRecord",
    "GatewayClient",
    "IntentClass",
    "NormalizedRequest",
    "OwnerGate",
    "OwnerGateError",
    "SteeringCommand",
    "TaskListFilter",
    "TaskRecord",
    "TaskRegistry",
    "approve_and_continue",
    "verify_task_consistency",
]
