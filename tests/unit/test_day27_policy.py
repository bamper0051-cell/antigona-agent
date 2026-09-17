"""Day 27: Policy engine tests."""
import pytest

from antigona.policy.engine import PolicyEngine


@pytest.mark.asyncio
async def test_policy_allows_default():
    eng = PolicyEngine(require_approval=False)
    result = await eng.check("filesystem_read", context={"user_id": "1", "session_id": "s"})
    assert result["allowed"] is True


@pytest.mark.asyncio
async def test_policy_requires_approval_by_default():
    eng = PolicyEngine(require_approval=True)
    result = await eng.check("terminal_run", context={"user_id": "1", "session_id": "s"})
    assert result["requires_approval"] is True


@pytest.mark.asyncio
async def test_policy_blocks_dangerous_shell():
    eng = PolicyEngine()
    result = await eng.check_shell("rm -rf /", context={"user_id": "1", "session_id": "s"})
    assert "critical" in result["reason"].lower() or "dangerous" in result["reason"].lower()



@pytest.mark.asyncio
async def test_policy_allows_safe_shell():
    eng = PolicyEngine(require_approval=False)
    result = await eng.check_shell("ls -la", context={"user_id": "1", "session_id": "s"})
    assert result["allowed"] is True


@pytest.mark.asyncio
async def test_policy_risk_levels():
    eng = PolicyEngine(require_approval=False)
    result = await eng.check_shell("ls", context={"user_id": "1", "session_id": "s"})
    assert isinstance(result["risk_level"], str)


@pytest.mark.asyncio
async def test_policy_blocks_mkfs():
    eng = PolicyEngine()
    result = await eng.check_shell("mkfs.ext4 /dev/sda", context={"user_id": "1", "session_id": "s"})
    assert result["allowed"] is False
