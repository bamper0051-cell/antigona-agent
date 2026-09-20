"""DF-WO2-003-full — approved shell/write execution via the canonical fence.

LIVE BLOCKER this package closes: with ``ANTIGONA_OWNERSHIP_ENABLED=1`` the
model/dialogue path (``sandbox.shell`` and the registry ``write_file`` handler)
ran with NO fencing token, so every approved action failed closed with
``... has no fencing token``.  The worker path forwarded a token; these surfaces
never minted one.

The fix mints a per-action token from the canonical authority
(``ownership.wiring.mint_execution_ownership``), binds it to exactly one action,
and releases it afterwards.  These regressions pin BOTH halves of the contract:

  * an APPROVED action now executes and produces a real outcome;
  * an UNAPPROVED / foreign / expired / already-released token is still DENIED
    fail-closed with no side effect (INV-04/INV-06);
  * a token is never reusable across actions;
  * the sandbox execution path stays narrowly scoped (allowlisted proxy) and the
    hardened service units gain no privilege.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import stat
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.engine.unified_executor import ToolExecutionRequest, UnifiedToolExecutionLayer
from antigona.ownership.central import CentralAuthority
from antigona.ownership.epoch import EpochLedger
from antigona.ownership.identity import resolve_repo_identity
from antigona.ownership.wiring import (
    mint_execution_ownership,
    release_workspace_ownership,
    shared_ledger_path,
)
from antigona.shell import DockerShellTool, ShellInput, ToolResult
from antigona.tools.registry import ToolRegistry, register_builtins

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SYSTEMD = Path("/var/lib/antigona-deployment/systemd")


@pytest.fixture(autouse=True)
def _workspace_env_matches_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror production wiring: the workspace root IS ``ANTIGONA_WORKSPACE``.

    ``paths.workspace_dir()`` — the root the PolicyEngine fences against — reads that
    variable, while this module builds ``Settings(workspace=tmp_path / "workspace")``.
    Without the variable the two roots diverge, so an in-workspace write is classified
    as an out-of-workspace mutation that correctly demands an owner grant.  The service
    units export the variable; the test must too, or it stops modelling production.
    """
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path / "workspace",
        workspace_backend="local",
        ownership_enabled=True,
        ownership_dir=tmp_path / "ownership",
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_builtins(registry)
    return registry


def _other_owner_takes_over(settings: Settings, repo_root: Path, ctx: object) -> EpochLedger:
    """Supersede an EXPIRED lease with a NEW epoch held by another owner."""
    repo_uuid = resolve_repo_identity(repo_root).repo_uuid
    ledger = EpochLedger(shared_ledger_path(repo_uuid, settings))
    authority = CentralAuthority(ledger)
    expiry = ctx.ledger.lease_expiry(repo_uuid)  # type: ignore[attr-defined]
    authority.takeover(repo_uuid, "other-owner", lease_seconds=3600, now=expiry + timedelta(seconds=1))
    return ledger


# ── 1. registry.write_file: approved write actually lands ────────────────────


def test_approved_write_file_creates_file_inside_workspace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ctx = mint_execution_ownership(settings.workspace, settings=settings)
    assert ctx is not None
    target = settings.workspace / "approved.txt"
    try:
        out = asyncio.run(
            _registry().dispatch(
                "write_file", path=str(target), content="approved", _ownership=ctx
            )
        )
    finally:
        release_workspace_ownership(ctx)
    parsed = json.loads(out)
    assert parsed.get("success") is True, out
    assert target.is_file()
    assert target.read_text() == "approved"
    # inside the governed workspace, and never world-writable
    assert target.resolve().is_relative_to(settings.workspace.resolve())
    assert not (target.stat().st_mode & stat.S_IWOTH)


# ── 2. fail-closed: unchanged for unapproved / tokenless surfaces ────────────


def test_write_file_without_token_still_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNERSHIP_ENABLED", "1")
    target = tmp_path / "denied.txt"
    out = asyncio.run(_registry().dispatch("write_file", path=str(target), content="x"))
    parsed = json.loads(out)
    # Fail-closed refusal is what this regression pins.  An out-of-workspace write is also a
    # mutation the policy flags ``requires_approval`` (P1-002 parity with KernelExecutor), so
    # the approval gate may answer before the ownership fence; either refusal is valid.
    reason = str(parsed.get("error", ""))
    assert parsed.get("success") is not True, out
    assert "denied" in reason.lower() or parsed.get("requires_approval") is True, out
    assert not target.exists()  # INV-04: no side effect


def test_write_file_with_foreign_token_denied(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ctx = mint_execution_ownership(settings.workspace, settings=settings)
    assert ctx is not None
    ledger = _other_owner_takes_over(settings, settings.workspace, ctx)
    target = settings.workspace / "foreign.txt"
    try:
        out = asyncio.run(
            _registry().dispatch(
                "write_file", path=str(target), content="x", _ownership=ctx
            )
        )
        parsed = json.loads(out)
        assert "denied" in str(parsed.get("error", "")).lower(), out
        assert not target.exists()
    finally:
        ctx.ledger.close()
        ledger.close()


def test_released_token_not_accepted_across_actions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ctx = mint_execution_ownership(settings.workspace, settings=settings)
    assert ctx is not None
    first = settings.workspace / "first.txt"
    try:
        out1 = asyncio.run(
            _registry().dispatch("write_file", path=str(first), content="1", _ownership=ctx)
        )
        assert json.loads(out1).get("success") is True, out1
        # release == end of THAT action; the same token must not authorize another
        release_workspace_ownership(ctx)
        second = settings.workspace / "second.txt"
        out2 = asyncio.run(
            _registry().dispatch("write_file", path=str(second), content="2", _ownership=ctx)
        )
        assert "denied" in str(json.loads(out2).get("error", "")).lower(), out2
        assert not second.exists()
    finally:
        ctx.ledger.close()


def test_expired_token_denied(tmp_path: Path) -> None:
    """A token whose lease has elapsed is denied (TTL semantics preserved)."""
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ctx = mint_execution_ownership(settings.workspace, settings=settings, lease_seconds=0)
    assert ctx is not None
    try:
        from antigona.ownership.epoch import FenceDeniedError

        future = ctx.ledger.lease_expiry(ctx.repo_uuid) + timedelta(seconds=5)
        assert ctx.central is not None
        with pytest.raises(FenceDeniedError):
            ctx.central.acquire_write_permit(
                ctx.repo_uuid, ctx.owner_id, ctx.fencing_epoch, now=future
            )
        target = settings.workspace / "expired.txt"
        out = asyncio.run(
            _registry().dispatch("write_file", path=str(target), content="x", _ownership=ctx)
        )
        assert "denied" in str(json.loads(out).get("error", "")).lower(), out
    finally:
        ctx.ledger.close()


# ── 3. unified executor (the dialogue/model surface): mint → bind → release ──


class _RecordingShellSurface:
    """Stands in for DockerShellTool and records the token it was given."""

    def __init__(self) -> None:
        self.ownership = None
        self.calls: list[object] = []
        self.fenced_ok: list[bool] = []

    def bind_ownership(self, ownership: object) -> None:
        self.ownership = ownership

    def execute(self, arguments: ShellInput) -> ToolResult:
        self.calls.append(arguments)
        from antigona.ownership.wiring import enforce_write_fence

        allowed = True
        try:
            enforce_write_fence(self.ownership, "sandbox.shell")
        except Exception:
            allowed = False
        self.fenced_ok.append(allowed)
        if not allowed:
            return ToolResult(False, "failed", error="protected execution denied")
        return ToolResult(True, "completed", {"output": "REAL-STDOUT"})


def _layer(tmp_path: Path, surface: object) -> UnifiedToolExecutionLayer:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    return UnifiedToolExecutionLayer(sandbox_shell_tool=surface, settings=settings)


def test_unified_shell_binds_a_live_token_then_releases(tmp_path: Path) -> None:
    surface = _RecordingShellSurface()
    layer = _layer(tmp_path, surface)
    req = ToolExecutionRequest(
        tool_name="sandbox.shell", params={"command": "echo hi"}, requester="dialogue"
    )
    out = asyncio.run(layer._execute_sandbox_shell("echo hi", req, "corr-1"))
    assert json.loads(out).get("success") is True, out
    assert surface.fenced_ok == [True]
    # the action ran through the REAL surface (not a stub bypass)
    assert len(surface.calls) == 1
    # one action == one token: the token is dropped afterwards (no reuse)
    assert surface.ownership is None


def test_unified_shell_second_action_mints_fresh_token(tmp_path: Path) -> None:
    surface = _RecordingShellSurface()
    layer = _layer(tmp_path, surface)
    req = ToolExecutionRequest(
        tool_name="sandbox.shell", params={"command": "echo hi"}, requester="dialogue"
    )
    for i in range(3):
        out = asyncio.run(layer._execute_sandbox_shell("echo hi", req, f"corr-{i}"))
        assert json.loads(out).get("success") is True, out
    assert surface.fenced_ok == [True, True, True]


def test_unified_shell_fails_closed_when_repo_held_by_another_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    # another live owner holds the repo -> mint must degrade to DENY-ALL
    repo_uuid = resolve_repo_identity(settings.workspace).repo_uuid
    ledger = EpochLedger(shared_ledger_path(repo_uuid, settings))
    CentralAuthority(ledger).acquire(repo_uuid, "other-owner", lease_seconds=3600)
    surface = _RecordingShellSurface()
    layer = UnifiedToolExecutionLayer(sandbox_shell_tool=surface, settings=settings)
    req = ToolExecutionRequest(
        tool_name="sandbox.shell", params={"command": "echo hi"}, requester="dialogue"
    )
    try:
        out = asyncio.run(layer._execute_sandbox_shell("echo hi", req, "corr-busy"))
        assert json.loads(out).get("success") is False, out
        assert surface.fenced_ok == [False]
    finally:
        ledger.close()


def test_unified_write_file_path_mints_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    layer = UnifiedToolExecutionLayer(settings=settings)
    target = settings.workspace / "dialogue-write.txt"
    req = ToolExecutionRequest(
        tool_name="write_file",
        params={"path": str(target), "content": "from-dialogue"},
        requester="dialogue",
    )
    out = asyncio.run(layer.execute(req))
    parsed = json.loads(out)
    assert parsed.get("success") is True, out
    assert target.read_text() == "from-dialogue"


# ── 4. DockerShellTool surface: bind + fence semantics ───────────────────────


def test_docker_shell_tool_bind_ownership_denies_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNERSHIP_ENABLED", "1")
    tool = DockerShellTool(tmp_path / "shell")
    tool.bind_ownership(None)
    res = tool.execute(ShellInput(command=("echo", "pwned")))
    assert not res.ok
    assert "no fencing token" in (res.error or "")


def test_docker_shell_tool_bind_ownership_accepts_live_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ctx = mint_execution_ownership(settings.workspace, settings=settings)
    assert ctx is not None
    tool = DockerShellTool(tmp_path / "shell")
    tool.bind_ownership(ctx)
    assert tool.ownership is ctx
    ctx.ledger.close()


# ── 5. sandbox execution path: narrowly scoped proxy + isolation assertions ──


def _load_proxy():
    path = REPO_ROOT / "deploy" / "sandbox" / "docker_socket_proxy.py"
    spec = importlib.util.spec_from_file_location("antigona_docker_socket_proxy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_allowlist_is_deny_by_default() -> None:
    proxy = _load_proxy()
    base = "/v1.55"
    allowed = [
        ("GET", "/_ping"),
        ("GET", f"{base}/containers/json"),
        ("POST", f"{base}/containers/create"),
        ("POST", f"{base}/containers/abc/start"),
        ("POST", f"{base}/containers/abc/wait"),
        ("GET", f"{base}/containers/abc/logs"),
        ("GET", f"{base}/containers/abc/json"),
        ("DELETE", f"{base}/containers/abc"),
        ("POST", f"{base}/containers/abc/attach"),
    ]
    denied = [
        ("POST", f"{base}/containers/abc/exec"),
        ("POST", f"{base}/images/create"),
        ("POST", f"{base}/build"),
        ("POST", f"{base}/volumes/create"),
        ("POST", f"{base}/networks/create"),
        ("GET", f"{base}/swarm"),
        ("POST", f"{base}/secrets/create"),
        ("GET", "/_ping/../../etc/passwd"),
        ("POST", f"{base}/containers/abc/../create-host-shell"),
    ]
    for method, target in allowed:
        assert proxy._allowed(method, target), (method, target)
    for method, target in denied:
        assert not proxy._allowed(method, target), (method, target)


def test_sandbox_profile_keeps_hardening_flags(tmp_path: Path) -> None:
    from antigona.sandbox.runner import SandboxProfile, build_run_argv

    profile = SandboxProfile(workspace=tmp_path, image="python:3.12-alpine", runtime="runc")
    argv = build_run_argv(profile, ["echo", "hi"])
    joined = " ".join(argv)
    for flag in (
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--network=none",
        "--runtime=runc",
    ):
        assert flag in joined, flag
    assert "docker.sock" not in joined


@pytest.mark.skipif(not DEPLOY_SYSTEMD.exists(), reason="deployment package not installed")
def test_no_privilege_widening_in_service_units() -> None:
    app_units = [
        "antigona-bot.service",
        "antigona-delivery.service",
        "antigona-gateway.service",
        "antigona-orchestration.service",
        "antigona-verifier.service",
        "antigona-worker.service",
    ]
    for name in app_units:
        text = (DEPLOY_SYSTEMD / name).read_text()
        assert "SupplementaryGroups=docker" not in text, name
        assert "NoNewPrivileges=yes" in text, name
        assert "CapabilityBoundingSet=" in text, name  # empty == drop all
        assert "ProtectSystem=strict" in text, name
    proxy = (DEPLOY_SYSTEMD / "antigona-docker-proxy.service").read_text()
    assert "SupplementaryGroups=docker" in proxy
    assert "NoNewPrivileges=yes" in proxy
    assert "ReadWritePaths=/run/antigona" in proxy
    # sandbox-executing services reach the daemon only through the proxy socket
    for name in ("antigona-worker.service", "antigona-gateway.service", "antigona-bot.service"):
        text = (DEPLOY_SYSTEMD / name).read_text()
        assert "Environment=DOCKER_HOST=unix:///run/antigona/docker.sock" in text, name
        assert "antigona-docker-proxy.service" in text, name


# ── 6. real end-to-end: an approved shell command returns REAL output ────────


def _docker_ready() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "python:3.12-alpine"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="docker daemon or sandbox image unavailable")
def test_approved_shell_command_executes_and_returns_real_output(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    shell = DockerShellTool(settings.workspace, "python:3.12-alpine", runtime="runc")
    layer = UnifiedToolExecutionLayer(sandbox_shell_tool=shell, settings=settings)
    req = ToolExecutionRequest(
        tool_name="sandbox.shell",
        params={"command": "echo FENCED-OK"},
        requester="dialogue",
    )
    out = asyncio.run(layer._execute_sandbox_shell("echo FENCED-OK", req, "corr-live"))
    parsed = json.loads(out)
    assert parsed.get("success") is True, out
    assert "FENCED-OK" in str(parsed.get("output", "")), out
    assert shell.ownership is None  # released after the single action
