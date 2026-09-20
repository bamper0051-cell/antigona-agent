"""DF-WO2-003-full — the LIVE write surface is fenced (GROK P0 recheck closure).

GROK re-verification (16_GROK_REVIEW_P0_RECHECK) found the gate was bound only
onto ``LocalWorkspace`` while the PRIMARY live worker writes through
``WorkspaceFileTool`` / ``DockerShellTool`` and the registry ``WRITE_FILE``
handler — all UNFENCED.  A stale owner could still write in production.

RED (before this wiring): ``WorkspaceFileTool(DockerSandboxBackend)`` had no
ownership fence; a stale owner's ``write`` SUCCEEDED and the file was created.
These tests are the LIVE-surface probe:

  * a STALE owner (superseded by a takeover on the shared ledger) is DENIED
    through EVERY live write surface — ``WorkspaceFileTool``, ``DockerShellTool``,
    the registry ``_handle_write_file``, and a remote/mock backend — and the
    target file is NEVER created (no side effect).
  * a LIVE (current) owner's write through the tool is PERMITTED (fence does
    not over-block).
  * an ownership-ENABLED surface carrying NO token fails CLOSED (INV-06).
  * ownership DISABLED (default) changes NOTHING on any surface (backward-compat).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.contracts import WriteFileInput
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.ownership.central import CentralAuthority
from antigona.ownership.epoch import EpochLedger, FenceDeniedError, OwnershipContext
from antigona.ownership.identity import resolve_repo_identity
from antigona.ownership.wiring import shared_ledger_path
from antigona.shell import DockerShellTool, ShellInput
from antigona.tools.registry import _handle_write_file
from antigona.workspace import DockerWorkspace, WorkspaceFactory


def _settings(root: Path) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        workspace=root,
        workspace_backend="local",
        ownership_enabled=True,
        ownership_dir=root / "shared",
    )


def _repo_uuid(root: Path) -> str:
    return resolve_repo_identity(root).repo_uuid


def _stale_context(root: Path, settings: Settings) -> tuple[OwnershipContext, list[Callable[[], None]]]:
    """Build an OwnershipContext that is now STALE (owner-A superseded by owner-B).

    Returns ``(stale_ctx, cleaners)`` where cleaners closes every open ledger.
    """
    ws = WorkspaceFactory.create_workspace(config=settings)
    ctx = getattr(ws, "_ownership", None)
    assert ctx is not None, "ownership was not bound"
    repo = _repo_uuid(root)
    ledger2 = EpochLedger(shared_ledger_path(repo, settings))
    authority2 = CentralAuthority(ledger2)
    authority2.takeover(
        repo,
        "owner-B",
        now=ctx.ledger.lease_expiry(repo) + timedelta(seconds=1),
    )
    cleaners: list[Callable[[], None]] = [ctx.ledger.close, ledger2.close]
    return ctx, cleaners


def _close(cleaners: list[Callable[[], None]]) -> None:
    for c in cleaners:
        try:
            c()
        except Exception:
            pass


# ── 1. WorkspaceFileTool (the primary Orchestrator workspace.write_text path) ─


def test_filetool_fences_stale_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ctx, cleaners = _stale_context(tmp_path, settings)
    try:
        backend = InProcessTestBackend(tmp_path / "ws", test_mode=True, ownership=ctx)
        tool = WorkspaceFileTool(backend, ownership=ctx)
        res = tool.execute(WriteFileInput(path="stale.txt", content="x"))
        assert not res.ok
        assert "denied" in (res.error or "").lower()
        # INV-04: provably no side effect through the live tool surface.
        assert not (tmp_path / "ws" / "stale.txt").exists()
    finally:
        _close(cleaners)


def test_filetool_live_owner_permitted(tmp_path: Path) -> None:
    """A current owner's write through the live tool is NOT over-blocked."""
    settings = _settings(tmp_path)
    ws = WorkspaceFactory.create_workspace(config=settings)
    ctx = getattr(ws, "_ownership", None)
    assert ctx is not None
    try:
        backend = InProcessTestBackend(tmp_path / "ws", test_mode=True, ownership=ctx)
        tool = WorkspaceFileTool(backend, ownership=ctx)
        res = tool.execute(WriteFileInput(path="ok.txt", content="ok"))
        assert res.ok
        assert (tmp_path / "ws" / "ok.txt").exists()
    finally:
        ctx.ledger.close()


# ── 2. DockerShellTool (arbitrary commands can write the workspace) ──────────


def test_shelltool_fences_stale_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ctx, cleaners = _stale_context(tmp_path, settings)
    try:
        shell = DockerShellTool(tmp_path / "shell", ownership=ctx)
        res = shell.execute(ShellInput(command=("touch", "pwned.txt")))
        assert not res.ok
        assert "denied" in (res.error or "").lower()
        # The fence runs BEFORE docker spawn / any host mutation.
        assert not (tmp_path / "shell" / "pwned.txt").exists()
    finally:
        _close(cleaners)


# ── 3. registry WRITE_FILE handler ───────────────────────────────────────────


def test_registry_write_file_fences_stale_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ctx, cleaners = _stale_context(tmp_path, settings)
    try:
        target = tmp_path / "reg.txt"
        out = asyncio.run(
            _handle_write_file(path=str(target), content="x", ownership=ctx)
        )
        data = json.loads(out)
        assert "denied" in data.get("error", "").lower()
        assert not target.exists()
    finally:
        _close(cleaners)


def test_registry_write_file_live_owner_permitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry handler has ONE writable root: the canonical workspace
    # (``ANTIGONA_WORKSPACE`` -> ``paths.workspace_dir()``), not the test's bare
    # ``tmp_path``.  Point the env at the same root so this test keeps measuring
    # the OWNERSHIP fence (a live owner is permitted) rather than the
    # path-authorization rule (A-CORE-001: an absolute out-of-workspace write
    # needs a consumed owner grant).
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path))
    settings = _settings(tmp_path)
    ws = WorkspaceFactory.create_workspace(config=settings)
    ctx = getattr(ws, "_ownership", None)
    assert ctx is not None
    try:
        target = tmp_path / "reg-ok.txt"
        out = asyncio.run(
            _handle_write_file(path=str(target), content="ok", ownership=ctx)
        )
        assert json.loads(out).get("success")
        assert target.exists()
    finally:
        ctx.ledger.close()


# ── 4. remote/mock backend ───────────────────────────────────────────────────


def test_remote_backend_fences_stale_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ctx, cleaners = _stale_context(tmp_path, settings)
    try:
        ws = DockerWorkspace(root_path=tmp_path / "remote")
        ws.bind_ownership(ctx)
        with pytest.raises(FenceDeniedError):
            ws.write_file("stale.txt", "x")
        assert not (tmp_path / "remote" / "stale.txt").exists()
    finally:
        _close(cleaners)


# ── 5. fail-closed: enabled surface with NO token is DENIED (INV-06) ─────────


def test_enabled_no_token_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_OWNERSHIP_ENABLED", "1")
    backend = InProcessTestBackend(tmp_path / "w", test_mode=True, ownership=None)
    tool = WorkspaceFileTool(backend, ownership=None)
    res = tool.execute(WriteFileInput(path="x.txt", content="x"))
    assert not res.ok
    assert "no fencing token" in (res.error or "")
    assert not (tmp_path / "w" / "x.txt").exists()


# ── 6. backward-compat: ownership OFF changes NOTHING on any surface ─────────


def test_disabled_is_unfenced_across_all_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTIGONA_OWNERSHIP_ENABLED", raising=False)
    # The registry WRITE_FILE handler writes only inside the canonical workspace
    # (A-CORE-001), so point ``ANTIGONA_WORKSPACE`` at the surface's root; this
    # test measures "ownership OFF changes nothing", not path authorization.
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path))
    # WorkspaceFileTool
    backend = InProcessTestBackend(tmp_path / "w", test_mode=True, ownership=None)
    tool = WorkspaceFileTool(backend, ownership=None)
    res = tool.execute(WriteFileInput(path="a.txt", content="a"))
    assert res.ok
    assert (tmp_path / "w" / "a.txt").exists()
    # registry WRITE_FILE
    target = tmp_path / "r.txt"
    out = asyncio.run(_handle_write_file(path=str(target), content="r"))
    assert json.loads(out).get("success")
    assert target.exists()
    # remote/mock backend
    ws = DockerWorkspace(root_path=tmp_path / "remote")
    ws.bind_ownership(None)
    ws.write_file("b.txt", "b")
    assert (tmp_path / "remote" / "b.txt").exists()
    # TaskRuntime
    from antigona.task.runtime import TaskRuntime

    tr = TaskRuntime(workspace=tmp_path / "tr_ws")
    t = tr.create_task("goal", [{"action_type": "WRITE_FILE", "path": "tr_out.txt", "content": "tr_content"}])
    step_res = tr.execute_next_step(t.id)
    assert step_res["success"] is True
    assert (tmp_path / "tr_ws" / "tr_out.txt").exists()


# ── 7. TaskRuntime WRITE_FILE surface fencing (W6-3) ─────────────────────────


def test_task_runtime_fences_stale_owner(tmp_path: Path) -> None:
    from antigona.task.runtime import TaskRuntime

    settings = _settings(tmp_path)
    ctx, cleaners = _stale_context(tmp_path, settings)
    try:
        tr = TaskRuntime(workspace=tmp_path / "tr_ws", ownership=ctx)
        t = tr.create_task("goal", [{"action_type": "WRITE_FILE", "path": "stale_tr.txt", "content": "data"}])
        step_res = tr.execute_next_step(t.id)
        assert step_res["success"] is False
        assert "denied" in step_res.get("error", "").lower()
        assert not (tmp_path / "tr_ws" / "stale_tr.txt").exists()
    finally:
        _close(cleaners)


def test_task_runtime_live_owner_permitted(tmp_path: Path) -> None:
    from antigona.task.runtime import TaskRuntime

    settings = _settings(tmp_path)
    ws = WorkspaceFactory.create_workspace(config=settings)
    ctx = getattr(ws, "_ownership", None)
    assert ctx is not None
    try:
        tr = TaskRuntime(workspace=tmp_path / "tr_ws", ownership=ctx)
        t = tr.create_task("goal", [{"action_type": "WRITE_FILE", "path": "ok_tr.txt", "content": "ok_data"}])
        step_res = tr.execute_next_step(t.id)
        assert step_res["success"] is True
        assert (tmp_path / "tr_ws" / "ok_tr.txt").exists()
    finally:
        ctx.ledger.close()


def test_task_runtime_enabled_no_token_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from antigona.task.runtime import TaskRuntime

    monkeypatch.setenv("ANTIGONA_OWNERSHIP_ENABLED", "1")
    tr = TaskRuntime(workspace=tmp_path / "tr_ws", ownership=None)
    t = tr.create_task("goal", [{"action_type": "WRITE_FILE", "path": "no_token.txt", "content": "data"}])
    step_res = tr.execute_next_step(t.id)
    assert step_res["success"] is False
    assert "no fencing token" in step_res.get("error", "").lower()
    assert not (tmp_path / "tr_ws" / "no_token.txt").exists()
