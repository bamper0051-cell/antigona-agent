"""Constitution P1-001 compatibility path: HIGH is denied pending approval."""
from __future__ import annotations

from pathlib import Path

import pytest

from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("write_file", {"path": "/etc/hosts"}),
        ("run_shell", {"tool_name": "run_shell", "command": "rm /tmp/constitutional-target"}),
    ],
)
async def test_high_risk_is_denied_and_requires_approval(
    tmp_path: Path, action: str, params: dict[str, str]
) -> None:
    grants = ApprovalGrantStore(db_path=str(tmp_path / "grants.sqlite"))
    policy = PolicyEngine(require_approval=True, grant_store=grants)

    verdict = await policy.check(
        action,
        params=params,
        context={"channel": "cli", "user_id": "owner", "session_id": "constitutional"},
    )

    assert verdict["risk_level"] == "HIGH"
    assert verdict["allowed"] is False
    assert verdict["requires_approval"] is True
