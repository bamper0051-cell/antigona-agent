"""Milestone 0 (P0 Security) — Policy fail-closed regression tests.

Reproduce the OLD fail-open behaviour and prove it is gone:

* Old: an exception inside ``PolicyEngine.check`` (or a missing ``allowed``
  key in the verdict) let the tool/action execute anyway.
* New (fail-closed): any policy exception -> DENY / INTERNAL_ERROR, and the
  tool handler is never invoked.
"""
import json

import pytest

from antigona.policy.engine import PolicyEngine
from antigona.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_policy_check_fails_closed_on_risk_classifier_exception():
    """check() must DENY (never ALLOW) when internal classification raises."""
    eng = PolicyEngine(require_approval=False)

    def boom(*_a, **_k):
        raise RuntimeError("risk classifier exploded")

    eng.risk_classifier.classify = boom  # type: ignore[method-assign]
    # Provide identity so the check reaches the classifier (the BUG-17
    # fail-closed identity gate is covered by its own test below).
    verdict = await eng.check(
        "filesystem_read", context={"user_id": "42", "session_id": "s"}
    )
    assert verdict["allowed"] is False
    assert "INTERNAL_ERROR" in verdict["reason"]
    assert verdict.get("error", "").startswith("POLICY_INTERNAL_ERROR")
    assert verdict["requires_approval"] is True


@pytest.mark.asyncio
async def test_policy_check_fails_closed_on_owner_override_exception():
    """check() must DENY when the owner-override lookup raises."""
    eng = PolicyEngine(require_approval=False)

    class _BoomOverride:
        def is_elevated(self, *_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("override store exploded")

    eng.owner_override = _BoomOverride()  # type: ignore[assignment]
    verdict = await eng.check("terminal_run", context={"channel": "cli", "user_id": "owner"})
    assert verdict["allowed"] is False
    assert "INTERNAL_ERROR" in verdict["reason"]


@pytest.mark.asyncio
async def test_registry_dispatch_denies_on_policy_exception_not_executes():
    """The OLD fail-open: a policy exception must NOT fall through to the handler."""
    registry = ToolRegistry()
    executed = {"ran": False}

    async def handler(**_kwargs) -> str:  # type: ignore[no-untyped-def]
        executed["ran"] = True
        return json.dumps({"ok": True})

    registry.register(
        "write_file",
        toolset="filesystem",
        schema={},
        handler=handler,
    )

    class _BoomPolicy:
        async def check(self, *_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("policy engine crashed mid-check")

    registry.policy_engine = _BoomPolicy()  # type: ignore[attr-defined]

    result = json.loads(await registry.dispatch("write_file", path="secret.txt"))

    # fail-closed: error surfaced, tool NOT executed
    assert "POLICY_INTERNAL_ERROR" in result.get("error", "")
    assert result.get("requires_approval") is True
    assert executed["ran"] is False, "handler must NOT run when policy raises"


@pytest.mark.asyncio
async def test_registry_dispatch_denies_when_verdict_lacks_allowed_key():
    """A verdict without an explicit ``allowed`` must be treated as DENY."""
    registry = ToolRegistry()
    executed = {"ran": False}

    async def handler(**_kwargs) -> str:  # type: ignore[no-untyped-def]
        executed["ran"] = True
        return json.dumps({"ok": True})

    registry.register("shell_run", toolset="shell", schema={}, handler=handler)

    class _NoAllowedPolicy:
        async def check(self, *_a, **_k):  # type: ignore[no-untyped-def]
            return {"requires_approval": True}  # no "allowed" key

    registry.policy_engine = _NoAllowedPolicy()  # type: ignore[attr-defined]

    result = json.loads(await registry.dispatch("shell_run", command="ls"))
    assert executed["ran"] is False
    assert "error" in result
