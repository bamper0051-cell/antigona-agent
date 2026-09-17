"""Unit tests for safe workspace file write auto-approval (D3).

Verifies:
- safe in-workspace write_text / write_file / create_file -> allowed, requires_approval=False, risk LOW
- escape paths (../etc/passwd, $HOME/x, /etc/passwd) -> requires_approval=True / denied (HIGH/SENSITIVE)
- sensitive files (.env, secret.key, id_rsa) -> requires_approval=True / denied (HIGH)
- critical actions (rm -rf, mkfs, drop database) -> blocked / CRITICAL 2-step confirmation
- dynamic workspace root resolution via ANTIGONA_WORKSPACE
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core.paths import home_dir
from antigona.policy.engine import ActionCategory, PolicyEngine
from antigona.security.risk_classifier import RiskClassifier, RiskLevel

CTX = {"channel": "cli", "user_id": "owner", "session_id": "test-session"}

# Derived once from the canonical home helper (never a hardcoded personal path);
# "/opt/antigona-home" is a HIGH_RISK_PATHS prefix, so on the canonical host the values below
# are identical to the historical literals.
_HOME = str(home_dir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "path"),
    [
        ("write_text", "output.txt"),
        ("write_text", "data/records.json"),
        ("write_file", "script.py"),
        ("write_file", "sub/deep/module.py"),
        ("workspace.write_text", "notes.md"),
        ("workspace_write", "report.csv"),
        ("create_file", "index.html"),
        ("append_file", "log.txt"),
        ("edit", "config.xml"),
        ("edit_file", "src/main.rs"),
        ("update_file", "build.gradle"),
        ("filesystem.write", "app.js"),
    ],
)
async def test_safe_workspace_write_is_auto_allowed_low_risk(
    action: str, path: str
) -> None:
    """Safe writes inside workspace must be SAFE / LOW risk and not require approval."""
    policy = PolicyEngine(require_approval=True)
    verdict = await policy.check(
        action,
        params={"path": path, "content": "print('hello')"},
        context=CTX,
    )

    assert verdict["allowed"] is True
    assert verdict["requires_approval"] is False
    assert verdict["risk_level"] == "LOW"
    assert verdict["category"] == ActionCategory.SAFE.value
    assert verdict["requires_2step_confirmation"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "escape_path"),
    [
        ("write_text", "/etc/passwd"),
        ("write_file", "/etc/hosts"),
        ("create_file", f"{_HOME}/x"),
        ("write_text", f"{_HOME}/secret.txt"),
        ("write_text", "../etc/passwd"),
        ("write_file", f"../..{_HOME}/x"),
        ("write_text", "/tmp/outside/malicious.sh"),
        ("write_text", "/var/log/test.log"),
        ("write_text", "/boot/vmlinuz"),
    ],
)
async def test_escape_paths_are_denied_or_require_approval(
    action: str, escape_path: str
) -> None:
    """Paths escaping the workspace or targeting system roots must be denied/HIGH risk."""
    policy = PolicyEngine(require_approval=True)
    verdict = await policy.check(
        action,
        params={"path": escape_path, "content": "evil"},
        context=CTX,
    )

    assert verdict["risk_level"] == "HIGH"
    assert verdict["allowed"] is False
    assert verdict["requires_approval"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "sensitive_path"),
    [
        ("write_text", ".env"),
        ("write_file", ".envrc"),
        ("create_file", "sub/.env.production"),
        ("write_text", "server.key"),
        ("write_file", "cert.pem"),
        ("write_text", "id_rsa"),
        ("write_file", ".ssh/authorized_keys"),
        ("write_text", "api_token.secret"),
    ],
)
async def test_sensitive_files_in_workspace_require_approval(
    action: str, sensitive_path: str
) -> None:
    """Writing secrets/credentials/env files even inside workspace must require approval."""
    policy = PolicyEngine(require_approval=True)
    verdict = await policy.check(
        action,
        params={"path": sensitive_path, "content": "SECRET=val"},
        context=CTX,
    )

    assert verdict["risk_level"] == "HIGH"
    assert verdict["allowed"] is False
    assert verdict["requires_approval"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cmd",),
    [
        ("rm -rf /",),
        (f"rm -rf {_HOME}/target",),
        ("rm -fr /var/data",),
        ("drop database test_db",),
        ("mkfs.ext4 /dev/sda",),
    ],
)
async def test_critical_commands_require_2step_confirmation(cmd: str) -> None:
    """CRITICAL actions must remain blocked and require 2-step confirmation."""
    policy = PolicyEngine(require_approval=True)
    verdict = await policy.check(
        "run_shell",
        params={"command": cmd},
        context=CTX,
    )

    assert verdict["allowed"] is False
    assert verdict["requires_approval"] is True
    assert verdict["requires_2step_confirmation"] is True
    assert verdict["risk_level"] == "CRITICAL"
    assert verdict["category"] == ActionCategory.CRITICAL.value
    assert "pending_confirmation" in verdict


@pytest.mark.asyncio
async def test_custom_workspace_environment_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dynamic workspace root from ANTIGONA_WORKSPACE is respected by policy."""
    custom_ws = tmp_path / "custom_ws"
    custom_ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(custom_ws))

    policy = PolicyEngine(require_approval=True)

    # Relative path resolves inside custom_ws -> LOW/SAFE
    verdict_rel = await policy.check(
        "write_text",
        params={"path": "local.json", "content": "{}"},
        context=CTX,
    )
    assert verdict_rel["allowed"] is True
    assert verdict_rel["requires_approval"] is False
    assert verdict_rel["risk_level"] == "LOW"

    # Absolute path inside custom_ws -> LOW/SAFE
    abs_inside = str(custom_ws / "sub" / "file.txt")
    verdict_abs = await policy.check(
        "write_file",
        params={"path": abs_inside, "content": "test"},
        context=CTX,
    )
    assert verdict_abs["allowed"] is True
    assert verdict_abs["requires_approval"] is False
    assert verdict_abs["risk_level"] == "LOW"

    # Path outside custom_ws -> requires approval (not auto-allowed)
    abs_outside = str(tmp_path / "outside.txt")
    verdict_out = await policy.check(
        "write_file",
        params={"path": abs_outside, "content": "test"},
        context=CTX,
    )
    assert verdict_out["requires_approval"] is True
    assert verdict_out["risk_level"] in ("MEDIUM", "HIGH")

    # System escape path -> denied / HIGH
    verdict_sys = await policy.check(
        "write_file",
        params={"path": "/etc/passwd", "content": "test"},
        context=CTX,
    )
    assert verdict_sys["allowed"] is False
    assert verdict_sys["requires_approval"] is True
    assert verdict_sys["risk_level"] == "HIGH"


def test_classifier_direct_methods() -> None:
    """Direct checks on RiskClassifier and PolicyEngine classification methods."""
    classifier = RiskClassifier()
    policy = PolicyEngine()

    # Safe write -> LOW / SAFE
    assert classifier.classify("write_text", path="doc.txt") == RiskLevel.LOW
    assert classifier.classify("workspace.write_text", path="a.py") == RiskLevel.LOW
    assert classifier.classify("CREATE_FILE", path="sub/b.json") == RiskLevel.LOW
    assert policy.classify_action_category("write_text", path="doc.txt") == ActionCategory.SAFE

    # Escape write -> HIGH / SENSITIVE
    assert classifier.classify("write_text", path="/etc/passwd") == RiskLevel.HIGH
    assert classifier.classify("write_file", path=f"{_HOME}/x") == RiskLevel.HIGH
    assert classifier.classify("write_text", path="../etc/passwd") == RiskLevel.HIGH
    assert policy.classify_action_category("write_text", path="/etc/passwd") == ActionCategory.SENSITIVE

    # Critical shell -> CRITICAL / CRITICAL
    assert classifier.classify("run_shell", content="rm -rf /") == RiskLevel.CRITICAL
    assert policy.classify_action_category("run_shell", command="rm -rf /") == ActionCategory.CRITICAL

    # Non-sensitive absolute path outside workspace -> MEDIUM (not HIGH, not requiring OTP/PIN)
    assert classifier.classify("WRITE_FILE", path="/tmp/test.txt") == RiskLevel.MEDIUM
    assert classifier.classify("WRITE_FILE", path="/tmp/test.txt") != RiskLevel.HIGH
    assert classifier.requires_otp(classifier.classify("WRITE_FILE", path="/tmp/test.txt")) is False

    # Reads outside the configured workspace are sensitive and gated.
    assert classifier.classify("read_file", path="/etc/hosts") == RiskLevel.HIGH
    assert policy.classify_action_category("read_file", path="/etc/hosts") == ActionCategory.SENSITIVE


def test_evaluate_risk_workspace_write_auto() -> None:
    """evaluate_risk must return LOW for safe in-workspace writes and HIGH for escape/sensitive paths."""
    from antigona.worker.hitl import (
        ConfirmationPolicy,
        evaluate_risk,
    )
    from antigona.worker.hitl import (
        RiskLevel as WorkerRiskLevel,
    )

    policy = ConfirmationPolicy()

    # In-workspace write -> LOW (auto-approvable)
    risk_low, _ = evaluate_risk("workspace.write_text", {"path": "a.txt"})
    assert risk_low == WorkerRiskLevel.LOW
    assert policy.should_auto_approve(risk_low) is True
    assert policy.should_require_approval(risk_low) is False

    # Non-sensitive absolute path -> MEDIUM (not HIGH, auto-approved under HIGH_ONLY)
    risk_tmp, _ = evaluate_risk("workspace.write_text", {"path": "/tmp/test.txt"})
    assert risk_tmp == WorkerRiskLevel.MEDIUM
    assert risk_tmp != WorkerRiskLevel.HIGH
    from antigona.worker.hitl import ConfirmationPolicyMode
    high_only_policy = ConfirmationPolicy(mode=ConfirmationPolicyMode.HIGH_ONLY)
    assert high_only_policy.should_require_approval(risk_tmp) is False
    assert high_only_policy.should_auto_approve(risk_tmp) is True

    # Escape / sensitive paths -> HIGH
    for dangerous_path in ("/etc/passwd", ".env", "../escape", f"{_HOME}/x"):
        risk_high, reason = evaluate_risk("workspace.write_text", {"path": dangerous_path})
        assert risk_high == WorkerRiskLevel.HIGH, f"Expected HIGH for {dangerous_path}, got {risk_high} ({reason})"
        assert policy.should_auto_approve(risk_high) is False
        assert policy.should_require_approval(risk_high) is True
