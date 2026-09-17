"""Antigona WORKSPACE OWNERSHIP v2.

Phase 2 (INV-02) provides a stable, durable, path-independent repository
identity (``repo_uuid``). Phase 3 (INV-03/INV-04) layers the durable monotonic
fencing epoch and the stale-writer gate on top of that identity. Later phases
add the central authority, lease/state machine, recovery and audit.
"""

from __future__ import annotations

from .central import CentralAuthority, CentralAuthorityFailure
from .epoch import (
    AcquireResult,
    AuditEvent,
    EpochLedger,
    FenceCheck,
    FenceDeniedError,
    FenceStatus,
    OwnershipBusyError,
    OwnershipCheck,
    OwnershipContext,
    WritePermit,
    default_ledger_path,
)
from .identity import (
    OwnershipIdentityError,
    RepoIdentity,
    repo_uuid_for_workspace,
    resolve_repo_identity,
)

__all__ = [
    "AcquireResult",
    "AuditEvent",
    "CentralAuthority",
    "CentralAuthorityFailure",
    "EpochLedger",
    "FenceCheck",
    "FenceDeniedError",
    "FenceStatus",
    "OwnershipBusyError",
    "OwnershipCheck",
    "OwnershipContext",
    "OwnershipIdentityError",
    "RepoIdentity",
    "WritePermit",
    "default_ledger_path",
    "repo_uuid_for_workspace",
    "resolve_repo_identity",
]
