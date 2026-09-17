"""DF-WO2-003-residual — the 2 residual live write surfaces are fenced.

GROK FINAL VERDICT (17_GROK_FINAL_VERDICT.md) closed the primary live write
surface (WorkspaceFileTool/DockerShellTool/registry/remote/agent_core) but found
TWO remaining unfenced direct filesystem mutations into the protected workspace
root in the live worker/orchestrator dispatch path:

1. ``worker/turn_executor.py`` ``_persist_artifact`` — ``target.write_bytes(payload)``
   writes ``settings.workspace/turn_results/<task_id>.md``.  ``TurnTaskExecutor``
   was constructed without an ownership token, so a stale/unauthorised owner
   executing a read-only turn task wrote into the protected root UNFENCED.
2. ``orchestrator.py`` ``_materialize_mcp_file_artifact`` — ``shutil.copy2`` writes
   ``settings.workspace/.antigona-results/<task_id>.mp3``, not routed through any
   fence.

RED (before this wiring): both surfaces wrote the file with ownership ON +
stale owner.  These tests are the residual-surface probe:

  * a STALE owner (superseded by a takeover on the shared ledger) is DENIED
    through BOTH residual surfaces and the target file is NEVER created.
  * a LIVE (current) owner is PERMITTED through both (fence does not over-block).
  * an ownership-ENABLED surface carrying NO token fails CLOSED (INV-06).
  * ownership DISABLED (default) changes NOTHING on either surface (backward-compat).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.ownership.central import CentralAuthority
from antigona.ownership.epoch import EpochLedger, FenceDeniedError, OwnershipContext
from antigona.ownership.identity import resolve_repo_identity
from antigona.ownership.wiring import shared_ledger_path
from antigona.repository import CreateTask, TaskRepository
from antigona.turn_bridge.turn_engine_adapter import TurnResult
from antigona.worker.turn_executor import TurnTaskExecutor
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
    """Build an OwnershipContext that is now STALE (owner-A superseded by owner-B)."""
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


def _to_running(repo: TaskRepository, task) -> None:
    """Mirror ``turn_executor._dispatch``: step must be RUNNING before persist."""
    from antigona.models import StepState

    repo.transition_step(task, task.steps[0], StepState.RUNNING, "test", "test")
    repo.session.commit()


class _StubRuntime:
    """Minimal stand-in for a live TurnRuntime (never invoked in these tests)."""

    def __init__(self) -> None:
        self.worker = None
        self.budget = None

    def close(self) -> None:  # pragma: no cover - close may or may not be called
        pass


# ── 1. turn_executor._persist_artifact → turn_results/<id>.md ────────────────


def test_turn_executor_fences_stale_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ctx, cleaners = _stale_context(tmp_path, settings)
    try:
        db = Database("sqlite:///:memory:")
        db.create_all()
        with db.session_factory() as session:
            repo = TaskRepository(session)
            task, _ = repo.create(
                CreateTask("owner", "turn goal", "proof.md", "data", "key-turn")
            )
            task.side_effect_key = "k-turn"
            session.commit()
            ex = TurnTaskExecutor(
                session,
                verifier=type("V", (), {})(),
                runtime=_StubRuntime(),
                workspace_root=tmp_path / "ws",
                lease_seconds=30,
                ownership=ctx,
            )
            with pytest.raises(FenceDeniedError):
                ex._persist_artifact(
                    task,
                    TurnResult(success=True, final_response="final"),
                    "corr",
                )
            # INV-04: provably no side effect through the residual turn surface.
            assert not (tmp_path / "ws" / "turn_results" / f"{task.id}.md").exists()
    finally:
        _close(cleaners)


def test_turn_executor_live_owner_permitted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ws = WorkspaceFactory.create_workspace(config=settings)
    ctx = getattr(ws, "_ownership", None)
    assert ctx is not None
    try:
        db = Database("sqlite:///:memory:")
        db.create_all()
        with db.session_factory() as session:
            repo = TaskRepository(session)
            task, _ = repo.create(
                CreateTask("owner", "turn goal", "proof.md", "data", "key-turn2")
            )
            task.side_effect_key = "k-turn2"
            _to_running(repo, task)
            ex = TurnTaskExecutor(
                session,
                verifier=type("V", (), {})(),
                runtime=_StubRuntime(),
                workspace_root=tmp_path / "ws",
                lease_seconds=30,
                ownership=ctx,
            )
            artifact = ex._persist_artifact(
                task,
                TurnResult(success=True, final_response="final"),
                "corr",
            )
            assert artifact is not None
            assert (tmp_path / "ws" / "turn_results" / f"{task.id}.md").exists()
    finally:
        ctx.ledger.close()


# ── 2. orchestrator._materialize_mcp_file_artifact → .antigona-results/<id>.mp3


def _mcp_result(file: Path):
    from antigona.contracts import ToolResult

    return ToolResult(True, "completed", data={"output": "x", "file": str(file)})


def test_orchestrator_mcp_fences_stale_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ctx, cleaners = _stale_context(tmp_path, settings)
    try:
        db = Database("sqlite:///:memory:")
        db.create_all()
        ws = DockerWorkspace(root_path=tmp_path / "ws")
        ws.bind_ownership(ctx)
        src = tmp_path / "ws" / "mcp_output" / "out.mp3"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"MP3DATA")
        with db.session_factory() as session:
            from antigona.orchestrator import Orchestrator

            task, _ = TaskRepository(session).create(
                CreateTask("owner", "mcp goal", "proof.mp3", "data", "key-mcp")
            )
            orch = Orchestrator(
                session,
                WorkspaceFileTool(InProcessTestBackend(tmp_path / "ws", test_mode=True)),
                verifier=type("V", (), {})(),
                workspace=ws,
            )
            res = orch._materialize_mcp_file_artifact(task, _mcp_result(src), tmp_path / "ws")
            assert not res.ok
            assert "denied" in (res.error or "").lower()
            assert not (tmp_path / "ws" / ".antigona-results" / f"{task.id}.mp3").exists()
    finally:
        _close(cleaners)


def test_orchestrator_mcp_live_owner_permitted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ws_factory = WorkspaceFactory.create_workspace(config=settings)
    ctx = getattr(ws_factory, "_ownership", None)
    assert ctx is not None
    try:
        db = Database("sqlite:///:memory:")
        db.create_all()
        ws = DockerWorkspace(root_path=tmp_path / "ws")
        ws.bind_ownership(ctx)
        src = tmp_path / "ws" / "mcp_output" / "out.mp3"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"MP3DATA")
        with db.session_factory() as session:
            from antigona.orchestrator import Orchestrator

            task, _ = TaskRepository(session).create(
                CreateTask("owner", "mcp goal", "proof.mp3", "data", "key-mcp2")
            )
            orch = Orchestrator(
                session,
                WorkspaceFileTool(InProcessTestBackend(tmp_path / "ws", test_mode=True)),
                verifier=type("V", (), {})(),
                workspace=ws,
            )
            res = orch._materialize_mcp_file_artifact(task, _mcp_result(src), tmp_path / "ws")
            assert res.ok
            assert (tmp_path / "ws" / ".antigona-results" / f"{task.id}.mp3").exists()
    finally:
        ctx.ledger.close()


# ── 3. fail-closed: enabled surface with NO token is DENIED (INV-06) ─────────


def test_turn_executor_enabled_no_token_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_OWNERSHIP_ENABLED", "1")
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        task, _ = TaskRepository(session).create(
            CreateTask("owner", "turn goal", "proof.md", "data", "key-nt")
        )
        task.side_effect_key = "k-nt"
        session.commit()
        ex = TurnTaskExecutor(
            session,
            verifier=type("V", (), {})(),
            runtime=_StubRuntime(),
            workspace_root=tmp_path / "ws",
            lease_seconds=30,
            ownership=None,
        )
        with pytest.raises(FenceDeniedError):
            ex._persist_artifact(
                task,
                TurnResult(success=True, final_response="final"),
                "corr",
            )
        assert not (tmp_path / "ws" / "turn_results" / f"{task.id}.md").exists()


# ── 4. backward-compat: ownership OFF changes NOTHING on either surface ──────


def test_disabled_is_unfenced_on_residual_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTIGONA_OWNERSHIP_ENABLED", raising=False)
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        task, _ = TaskRepository(session).create(
            CreateTask("owner", "turn goal", "proof.md", "data", "key-off")
        )
        task.side_effect_key = "k-off"
        _to_running(TaskRepository(session), task)
        # turn executor (no token, ownership off) writes fine
        ex = TurnTaskExecutor(
            session,
            verifier=type("V", (), {})(),
            runtime=_StubRuntime(),
            workspace_root=tmp_path / "ws",
            lease_seconds=30,
            ownership=None,
        )
        artifact = ex._persist_artifact(
            task,
            TurnResult(success=True, final_response="final"),
            "corr",
        )
        assert (tmp_path / "ws" / "turn_results" / f"{task.id}.md").exists()
        assert artifact is not None

        # orchestrator mcp (no token, ownership off) writes fine
        ws = DockerWorkspace(root_path=tmp_path / "ws")
        ws.bind_ownership(None)
        src = tmp_path / "ws" / "mcp_output" / "out.mp3"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"MP3DATA")
        from antigona.orchestrator import Orchestrator

        task2, _ = TaskRepository(session).create(
            CreateTask("owner", "mcp goal", "proof.mp3", "data", "key-off2")
        )
        orch = Orchestrator(
            session,
            WorkspaceFileTool(InProcessTestBackend(tmp_path / "ws", test_mode=True)),
            verifier=type("V", (), {})(),
            workspace=ws,
        )
        res = orch._materialize_mcp_file_artifact(task2, _mcp_result(src), tmp_path / "ws")
        assert res.ok
        assert (tmp_path / "ws" / ".antigona-results" / f"{task2.id}.mp3").exists()
