from __future__ import annotations

import logging
import os
import signal
import time
import urllib.error
import uuid

from antigona.config import Settings
from antigona.database import Database
from antigona.durable.state_cache import StateCache, build_state_cache
from antigona.filesystem import (
    DockerSandboxBackend,
    SandboxUnavailable,
    WorkspaceFileTool,
    WorkspaceViolation,
)
from antigona.lifecycle import ExecutionGuard
from antigona.models import QueueJob, TaskFlow, TaskState, utcnow
from antigona.observability import event, timed
from antigona.orchestrator import (
    BudgetLimitExceeded,
    DepthLimitExceeded,
    Orchestrator,
    SubagentError,
    aggregate_child_results,
    spawn_child_flow,
)
from antigona.queue import DurableQueue, TaskBroker, build_broker
from antigona.repository import ConcurrentUpdate, LeaseConflict, TaskRepository
from antigona.sandbox.runner import (
    ISOLATION_REFUSED,
    SandboxIsolationError,
    resolve_runtime,
)
from antigona.security import verifier_credential
from antigona.shell import DockerShellTool
from antigona.storage import ensure_storage_available
from antigona.turn_bridge.worker_integration import should_use_turn_worker
from antigona.verifier_client import VerifierClient
from antigona.worker.core import build_subagent_registry, select_adapter_for_task
from antigona.worker.turn_executor import (
    TurnTaskExecutor,
    TurnWorkerRuntime,
    build_turn_runtime,
)
from antigona.workspace import WorkspaceFactory

stop = False


def _worker_failure_code(exc: Exception) -> str:
    if isinstance(exc, (LeaseConflict, ConcurrentUpdate)):
        return "worker.lease_error"
    if isinstance(exc, TimeoutError):
        return "worker.timeout"
    if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError)):
        return "worker.verifier_error"
    if isinstance(exc, (SandboxUnavailable, WorkspaceViolation, OSError)):
        return "worker.tool_error"
    return "worker.execution_error"


def _handle_worker_failure(
    *,
    queue: DurableQueue,
    job: QueueJob,
    task: TaskFlow,
    max_retries: int,
    exc: Exception,
) -> str:
    """Persist and emit only a fixed category, never exception text.

    On exhausted retries the flow row is transitioned out of TOOL_EXECUTING /
    RUNNING into FAILED so the visible task status never stays stuck while the
    job is already FAILED (BUG ANT-001: TOOL_EXECUTING hang).
    """

    code = _worker_failure_code(exc)
    if job.attempts <= max_retries:
        queue.retry(job, code)
    else:
        job.status = "FAILED"
        job.last_error = code
        try:
            repo = TaskRepository(queue.session)
            if TaskState(task.status) in {TaskState.TOOL_EXECUTING, TaskState.RUNNING}:
                repo.transition(
                    task,
                    TaskState.FAILED,
                    f"worker retries exhausted: {code}",
                    "worker",
                    correlation_id=job.correlation_id,
                )
                repo.commit()
        except Exception:
            # The transition must never mask the original failure; the job
            # row is already FAILED, so worst case the flow is reconciled
            # by the startup watchdog (FIX-2).
            queue.session.rollback()
            queue.session.expire_all()
        queue.session.commit()
    event(
        "task_error",
        service="worker",
        correlation_id=job.correlation_id,
        task_id=task.id,
        session_id=task.owner_id,
        step_id=None,
        tool_name=None,
        status="error",
        error_code=code,
        error_category=code.removeprefix("worker."),
    )
    return code



def _cleanup_orphaned_sandbox_containers() -> None:
    """Remove throwaway ``antigona-*`` sandbox containers left by dead workers.

    When a worker dies mid-flow its ``docker run`` client is gone but the gVisor
    container keeps running, and a subsequent re-execution inherits that stale
    container's (possibly interrupted, non-zero) exit code — turning a recoverable
    flow into a hard FAILED. On startup the worker is the (single) owner of the
    throwaway containers, so it removes any leftovers so orphaned flows are
    re-executed fresh. Safe because the runtime is designed for one worker; a live
    worker would have these containers under its own process.
    """
    import subprocess as _subprocess
    try:
        out = _subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=antigona-"],
            capture_output=True, text=True, timeout=15,
        )
        ids = [x for x in (out.stdout or "").split() if x]
        if not ids:
            return
        for cid in ids:
            try:
                _subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=15)
            except Exception:
                pass
        event(
            "orphaned_sandbox_containers_cleaned",
            service="worker", correlation_id=None, task_id=None, session_id=None,
            step_id=None, tool_name=None, status="running", count=len(ids),
        )
    except Exception:
        pass


def _recover_stale_flows(db: Database) -> int:
    """Reconcile flows stuck in execution states with no live worker lease.

    FIX-2 (BUG ANT-001): a flow left in TOOL_EXECUTING/RUNNING whose lease has
    expired AND whose queue job is no longer RUNNING (FAILED/DONE/absent) can
    never be claimed again — ``DurableQueue.claim`` only picks QUEUED or
    RUNNING-with-expired-lease jobs. Such flows would hang forever; transition
    them to FAILED so the owner sees a terminal state instead of an eternal
    spinner. Returns the number of flows reconciled.
    """
    from sqlalchemy import select

    now = utcnow()
    reconciled = 0
    with db.session_factory() as session:
        repo = TaskRepository(session)
        flows = session.scalars(
            select(TaskFlow).where(
                TaskFlow.status.in_(["TOOL_EXECUTING", "RUNNING"]),
            )
        ).all()
        for task in flows:
            job = session.scalar(
                select(QueueJob).where(QueueJob.task_id == task.id).order_by(QueueJob.created_at.desc())
            )
            if job is not None and job.status == "QUEUED":
                # It is queued for (re-)claim — leave it alone.
                continue
            if (
                task.lease_expires_at is not None
                and task.lease_expires_at >= now
                and job is not None
                and job.status == "RUNNING"
            ):
                # Fresh lease with a live job — not stale.
                continue
            reason = (
                f"stale flow: no live worker job ({job.last_error})"
                if job is not None and job.last_error
                else "stale flow: no live worker job"
            )
            try:
                repo.transition(
                    task,
                    TaskState.FAILED,
                    reason,
                    "worker-watchdog",
                    correlation_id=job.correlation_id if job else None,
                )
                repo.commit()
                reconciled += 1
            except Exception:
                session.rollback()
                session.expire_all()
    if reconciled:
        event(
            "stale_flows_reconciled",
            service="worker", correlation_id=None, task_id=None, session_id=None,
            step_id=None, tool_name=None, status="warning", count=reconciled,
        )
    return reconciled


_ORPHAN_RECEIVED_GRACE_SECONDS = 30


def _recover_orphan_received_flows(
    db: Database,
    broker: TaskBroker | None = None,
    state_cache: StateCache | None = None,
    *,
    grace_seconds: int = _ORPHAN_RECEIVED_GRACE_SECONDS,
) -> int:
    """Drain flows accepted into RECEIVED but never enqueued.

    R1-QUEUE-01 (T0021 R1-B10): a ``TaskFlow`` committed in ``RECEIVED`` whose
    durable ``QueueJob`` was never created — the submission process died between
    ``repository.create()`` and ``DurableQueue.enqueue()``, or
    ``orchestrator.spawn_child_flow`` which never enqueues — is invisible to
    ``DurableQueue.claim`` (it selects ``QueueJob`` rows only) and is not handled
    by ``_recover_stale_flows`` (``TOOL_EXECUTING`` / ``RUNNING`` only). It hangs
    forever with no terminal or blocking state. Enqueue it so a worker runs it
    exactly once.

    Idempotent and race-safe: ``QueueJob.task_id`` is ``UNIQUE``, so a re-run —
    or a race with a live submission finishing its own ``enqueue()`` — creates no
    second job, and ``DurableQueue.claim``'s CAS on the single row guarantees one
    execution. ``grace_seconds`` skips very fresh rows so the sweep does not
    fight a submission still inside its ``create() -> enqueue()`` window.

    Returns the number of flows enqueued.
    """
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    cutoff = utcnow() - timedelta(seconds=max(grace_seconds, 0))
    recovered = 0
    with db.session_factory() as session:
        orphans = (
            session.scalars(
                select(TaskFlow)
                .where(
                    TaskFlow.status == TaskState.RECEIVED.value,
                    TaskFlow.created_at <= cutoff,
                    ~select(QueueJob.id)
                    .where(QueueJob.task_id == TaskFlow.id)
                    .exists(),
                )
                .order_by(TaskFlow.created_at)
            ).all()
        )
        queue = DurableQueue(session, broker, state_cache)
        for task in orphans:
            try:
                queue.enqueue(task)
                recovered += 1
            except IntegrityError:
                # A concurrent enqueue() won the UNIQUE(task_id) race — the flow
                # is already queued, nothing to do.
                session.rollback()
            except Exception:
                session.rollback()
                session.expire_all()
    if recovered:
        event(
            "orphan_received_flows_recovered",
            service="worker", correlation_id=None, task_id=None, session_id=None,
            step_id=None, tool_name=None, status="warning", count=recovered,
        )
    return recovered


_MAINTENANCE_SWEEP_INTERVAL_SECONDS = 10.0


def _run_worker_maintenance_sweep(
    db: Database,
    broker: TaskBroker | None = None,
    state_cache: StateCache | None = None,
    *,
    grace_seconds: int = _ORPHAN_RECEIVED_GRACE_SECONDS,
) -> int:
    """Run periodic maintenance sweeps during the worker loop (P1-05).

    Reconciles stale flows and drains expired orphan RECEIVED flows that were
    either skipped during startup grace window or created post-startup.
    """
    _recover_stale_flows(db)
    return _recover_orphan_received_flows(
        db, broker, state_cache, grace_seconds=grace_seconds
    )


def _resolve_worker_llm_config(settings: Settings) -> tuple[str, str, str]:
    """Resolve the worker's LLM runtime ``(base_url, api_key, model)`` from the
    ONE canonical authority — ``ProviderResolver`` / persisted
    ``provider_state.json`` via ``get_default_provider()`` — so the Gateway
    worker calls exactly the provider the operator selected with ``/setllm``.

    Fixes R1-PROVIDER-01 (T0021 R1-B04): the worker used to read raw
    ``PROVIDER_BASE_URL`` / ``DEEPSEEK_API_KEY`` / ``OPENROUTER_API_KEY`` env with
    a hardcoded DeepSeek default, so the selected provider and the provider the
    worker actually called could diverge and one provider's key could be sent to
    another provider's host.

    Fail-closed: if nothing resolves, raise instead of falling back to a
    hardcoded endpoint with a mismatched credential.
    """
    from antigona.conversation.provider_setup import get_default_provider

    provider = get_default_provider()
    if provider is None:
        raise RuntimeError(
            "no LLM provider resolved (provider_state.json / env / profiles all "
            "empty); the worker refuses to start without a canonical provider "
            "selection"
        )
    model = provider.model or settings.model_primary
    return provider.base_url, provider.api_key, model


def main() -> None:
    global stop
    settings = Settings.from_env()
    # P4.3 fail-closed: an unreachable durable layer is retried with backoff and
    # then aborts startup. A worker must never run "in the open" without state.
    ensure_storage_available(
        settings.async_db_url(),
        retries=settings.db_connect_retries,
        backoff_seconds=settings.db_connect_backoff_seconds,
    )
    db = Database(settings.database_url)
    db.create_all()
    # Redis is optional: build_broker(None) yields NullBroker (pure SQL polling),
    # build_state_cache(None) yields a disabled cache. Both degrade, never fail.
    broker = build_broker(settings.redis_url)
    state_cache = build_state_cache(
        settings.redis_url, ttl_seconds=settings.redis_state_ttl_seconds
    )
    credential = verifier_credential()
    verifier = VerifierClient(
        os.getenv("ANTIGONA_VERIFIER_URL", "http://127.0.0.1:8091"),
        credential,
    )
    # Fail-closed sandbox isolation (S-ISO-1): verify gVisor at STARTUP.  If it
    # is unavailable we do NOT silently run on runc — the runtime is pinned to
    # the refusal sentinel so every sandboxed command is refused, loudly.
    try:
        runtime = resolve_runtime(settings.sandbox_runtime)
    except SandboxIsolationError as exc:
        logging.getLogger("antigona").error(
            "sandbox isolation REFUSED at startup: %s", exc
        )
        runtime = ISOLATION_REFUSED
    event(
        "sandbox_runtime_resolved",
        service="worker",
        correlation_id=None,
        task_id=None,
        session_id=None,
        step_id=None,
        tool_name=None,
        status="running",
        runtime=runtime,
        preferred=settings.sandbox_runtime,
    )
    tool = WorkspaceFileTool(
        DockerSandboxBackend(settings.workspace, settings.docker_image, runtime),
        settings.tool_timeout_seconds,
    )
    shell = DockerShellTool(
        settings.workspace,
        # Install-capable shell profile: Debian slim (has BOTH apt and pip),
        # bridge network (reaches PyPI/apt registries), writable root (package
        # managers need to write). This relaxes the fail-closed default ONLY
        # for the worker's shell tool so "Install X"/"apt update"/"pip install"
        # actually succeed; the default DockerShellTool profile stays hardened
        # (alpine, network=none, read-only) everywhere else.
        "python:3.12-slim",
        # Bump past the 10s fast-command default so pip/apt installs don't
        # timeout mid-install (seen live: "pip install uv" → TIMEOUT).
        timeout_seconds=int(os.getenv("ANTIGONA_SHELL_TIMEOUT", "120")),
        runtime=runtime,
        network="bridge",
        read_only=False,
        # apt needs DAC_OVERRIDE (write /var/lib/apt) + FOWNER (chmod partial
        # dirs) + SETGID/SETUID under --cap-drop=ALL; all down-scoped to a
        # throwaway non-privileged container (no docker.sock, no host mounts
        # beyond the workspace). 1g because apt/dpkg OOM-kill at 128m.
        caps_add=("DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"),
        memory="1g",
    )
    # R1-PROVIDER-01: the worker's LLM endpoint, credential and model come from
    # the canonical ProviderResolver (persisted /setllm selection), never from
    # raw PROVIDER_BASE_URL / DEEPSEEK_API_KEY / OPENROUTER_API_KEY env.
    llm_base_url, llm_api_key, llm_model = _resolve_worker_llm_config(settings)
    event(
        "worker_llm_provider_resolved",
        service="worker", correlation_id=None, task_id=None, session_id=None,
        step_id=None, tool_name=None, status="running",
        base_url=llm_base_url, model=llm_model, credential_present=bool(llm_api_key),
    )
    turn_runtime: TurnWorkerRuntime = build_turn_runtime(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
        workspace_path=str(settings.workspace),
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=2,
        settings=settings,
    )
    worker = os.getenv("ANTIGONA_WORKER_ID", f"worker-{uuid.uuid4()}")

    def halt(_signum: int, _frame: object) -> None:
        global stop
        stop = True

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    event(
        "worker_started",
        service="worker",
        correlation_id=None,
        task_id=None,
        session_id=None,
        step_id=None,
        tool_name=None,
        status="running",
        worker_id=worker,
    )
    # Recover from a previous worker's death: drop throwaway sandbox containers
    # so orphaned flows are re-executed fresh instead of inheriting a stale exit.
    _cleanup_orphaned_sandbox_containers()
    # P1-05: run initial maintenance sweep at startup
    _run_worker_maintenance_sweep(db, broker, state_cache)

    # Scope 4 (health/readiness): liveness heartbeat so the Gateway's
    # aggregate /status can report this process as up without an HTTP port.
    from antigona.health.heartbeat import HeartbeatReporter

    worker_heartbeat = HeartbeatReporter("worker")
    worker_heartbeat.start()

    last_maintenance_sweep = time.monotonic()
    while not stop:
        worker_heartbeat.stamp_progress()
        now_mono = time.monotonic()
        if now_mono - last_maintenance_sweep >= _MAINTENANCE_SWEEP_INTERVAL_SECONDS:
            try:
                _run_worker_maintenance_sweep(db, broker, state_cache)
            except Exception:
                pass
            last_maintenance_sweep = now_mono
        with db.session_factory() as session:
            queue = DurableQueue(session, broker, state_cache)
            job = queue.claim(worker, settings.lease_seconds)
            if not job:
                # NullBroker.wait is the pre-P4.3 sleep; the Redis broker blocks
                # on BLPOP instead. Either way the claim above stays the arbiter.
                broker.wait(0.1)
                continue
            task = None
            guard = None
            try:
                task = TaskRepository(session).get(job.task_id)
                guard = ExecutionGuard(
                    db.session_factory,
                    job.id,
                    task.id,
                    worker,
                    settings.lease_seconds,
                    shell,
                )
                guard.start()
                workspace = WorkspaceFactory.create_workspace(
                    config=settings, task_id=task.id, owner_id=worker
                )
                # DF-WO2-003-full: forward the SAME live ownership fencing token
                # bound onto the workspace onto the shared write tool + shell so
                # EVERY live write/execute surface (WorkspaceFileTool,
                # DockerShellTool) is fenced when ownership is enabled (fail-closed,
                # INV-06).  When ownership is disabled the token is None and all
                # surfaces behave exactly as before (backward-compat).  Cleared in
                # the finally below so a stale token never leaks across tasks.
                _ctx = getattr(workspace, "ownership", None)
                tool.ownership = _ctx
                shell.ownership = _ctx
                with timed(
                    "task_run",
                    service="worker",
                    correlation_id=job.correlation_id,
                    task_id=task.id,
                    session_id=task.owner_id,
                    step_id=task.steps[0].id,
                    tool_name="workspace.write_text",
                ):
                    if should_use_turn_worker(task.tool_name):
                        result = TurnTaskExecutor(
                            session,
                            verifier,
                            runtime=turn_runtime,
                            workspace_root=settings.workspace,
                            lease_seconds=settings.lease_seconds,
                            state_cache=state_cache,
                            # DF-WO2-003-residual: forward the SAME live ownership
                            # token bound onto the workspace so the turn executor's
                            # artifact write is fenced too (fail-closed when
                            # enabled; None -> backward-compat).
                            ownership=_ctx,
                        ).run(task, worker)
                    else:
                        result = Orchestrator(
                            session,
                            tool,
                            verifier,
                            shell_tool=shell,
                            workspace=workspace,
                            lease_seconds=settings.lease_seconds,
                            max_retries=settings.max_retries,
                            state_cache=state_cache,
                        ).run(task, worker)
                if TaskState(result.status) in {TaskState.WAITING_APPROVAL}:
                    job.status = "WAITING"
                    session.commit()
                elif TaskState(result.status) is TaskState.DONE:
                    queue.finish(job)
                else:
                    # BUG ANT-003: queue.finish() unconditionally set DONE even
                    # when the flow terminated FAILED/TIMEOUT/BLOCKED — job and
                    # flow statuses drifted apart (job DONE, flow FAILED).
                    # Mirror the flow's terminal status onto the job row.
                    job.status = "FAILED" if TaskState(result.status) not in {
                        TaskState.CANCELLED,
                    } else "CANCELLED"
                    job.last_error = f"flow terminated: {result.status}"
                    session.commit()
            except Exception as exc:
                if task is None:
                    # The job's flow row is unreadable (deleted / corrupt): this is
                    # an orphaned job, not a retryable task failure. Mark it FAILED
                    # so it is not re-claimed forever and the worker keeps running.
                    job.status = "FAILED"
                    job.last_error = "worker.orphaned_job"
                    session.commit()
                    continue
                _handle_worker_failure(
                    queue=queue,
                    job=job,
                    task=task,
                    max_retries=settings.max_retries,
                    exc=exc,
                )
            finally:
                # DF-WO2-003-full: drop the per-task ownership token so the next
                # task starts clean (no stale fence carried over).
                tool.ownership = None
                shell.ownership = None
                from antigona.ownership.wiring import release_workspace_ownership

                release_workspace_ownership(locals().get("_ctx"))
                _ctx = None
                if guard is not None:
                    guard.stop()


__all__ = [
    "main",
    "spawn_child_flow",
    "aggregate_child_results",
    "SubagentError",
    "DepthLimitExceeded",
    "BudgetLimitExceeded",
    "build_subagent_registry",
    "select_adapter_for_task",
]
