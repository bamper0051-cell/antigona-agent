from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .config import Settings
from .database import Database
from .durable.state_machine import InvalidTransition
from .models import (
    Artifact,
    DeliveryOutbox,
    FlowStep,
    Skill,
    StateTransition,
    TaskFlow,
    TaskState,
    utcnow,
)
from .observability import event
from .repository import TaskRepository
from .result_safety import (
    is_sensitive_path,
    is_usable_result_text,
    sanitize_failure_reason,
    sanitize_result_text,
)
from .security import verifier_credential
from .skills import SkillIntegrityError, SkillsRegistry, SkillState
from .verifier import (
    HTTPVerifierProvider,
    LLMJudge,
    MissingVerifierCriteria,
    ProviderMalformedResponse,
    ProviderModelMismatch,
    ProviderTransportError,
    SkillVerificationError,
    VerifierCriteriaDatabase,
    VerifierCriteriaStore,
    detect_trajectory_anomalies,
    verify_skill_card_for_promotion,
)

logger = logging.getLogger("antigona.verifier_service")


class VerifyRequest(BaseModel):
    task_id: str
    correlation_id: str


class PromoteSkillRequest(BaseModel):
    revision: int
    correlation_id: str


def deterministic_expected_content(goal: str) -> str:
    """Ожидаемое содержимое файла для детерминированной write-цели.

    Возвращает exact content, вычислимый из текста цели (P0-031 FALSE_DONE
    guard), либо '' если цель не несёт детерминированного содержимого
    (LLM-draft/read/shell/compound — там контент не обязан совпадать).
    """
    from antigona.task_goal import parse_goal

    plan = parse_goal(goal or "")
    if plan.intent in ("file_write", "file_write_read") and plan.content:
        return plan.content
    return ""

def _open_artifact(workspace: Path, relative_path: str) -> tuple[int, list[int]]:
    """Open a single-link artifact without following symlinks in any path component."""
    if os.name == "nt":
        # Windows fallback: no dir_fd / O_CLOEXEC / O_NOFOLLOW / O_DIRECTORY.
        # Validate lexically, then open the file plainly (best effort).
        path = Path(relative_path)
        if (
            is_sensitive_path(relative_path)
            or path.is_absolute()
            or not path.parts
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise OSError("invalid artifact path")
        candidate = (workspace / path).resolve()
        if workspace.resolve() not in candidate.parents and candidate != workspace.resolve():
            raise OSError("invalid artifact path")
        # BUG ANT-007 (wave3, class V): os.open on Windows defaults to TEXT
        # mode — os.read stops at Ctrl-Z (0x1A) and binary artifacts (mp3 etc.)
        # read truncated, failing the size recheck ("artifact changed while
        # being read"). O_BINARY forces raw byte I/O.
        flags = os.O_RDONLY | (getattr(os, "O_BINARY", 0))
        return os.open(candidate, flags), []
    path = Path(relative_path)
    if (
        is_sensitive_path(relative_path)
        or path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise OSError("invalid artifact path")
    opened: list[int] = []
    o_cloexec = getattr(os, "O_CLOEXEC", 0)
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_directory = getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | o_cloexec | o_nofollow
    try:
        directory = os.open(workspace, flags | o_directory)
        opened.append(directory)
        for component in path.parts[:-1]:
            directory = os.open(component, flags | o_directory, dir_fd=directory)
            opened.append(directory)
        artifact_fd = os.open(path.parts[-1], flags, dir_fd=directory)
        artifact_stat = os.fstat(artifact_fd)
        if not stat.S_ISREG(artifact_stat.st_mode):
            os.close(artifact_fd)
            raise OSError("artifact is not a regular file")
        if artifact_stat.st_nlink != 1:
            os.close(artifact_fd)
            raise OSError("artifact hardlinks are not allowed")
        return artifact_fd, opened
    except Exception:
        for fd in reversed(opened):
            os.close(fd)
        raise


def read_artifact_safely(workspace: Path, relative_path: str, expected_size: int) -> bytes:
    """Fail closed on links, oversized files, and namespace/inode swaps during read-back.

    Regular artifacts must have exactly one link. This rejects hardlinks regardless of whether
    the other name is inside or outside the workspace, and the link count is rechecked after read.
    """
    if expected_size < 0:
        raise OSError("invalid artifact size")
    fd, directories = _open_artifact(workspace, relative_path)
    try:
        before = os.fstat(fd)
        if before.st_nlink != 1:
            raise OSError("artifact hardlinks are not allowed")
        if before.st_size != expected_size:
            raise OSError("artifact size mismatch")
        chunks: list[bytes] = []
        remaining = expected_size + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        if after.st_nlink != 1:
            if after.st_nlink == 0:
                raise OSError("artifact path changed while being read")
            raise OSError("artifact hardlinks are not allowed")
        if len(data) != expected_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
        ):
            raise OSError("artifact changed while being read")
    finally:
        os.close(fd)
        for directory in reversed(directories):
            os.close(directory)

    # Re-walk the current namespace after reading. This detects replacement of either
    # the final name or a parent component while the already-open descriptor was read.
    check_fd, check_directories = _open_artifact(workspace, relative_path)
    try:
        current = os.fstat(check_fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
            current.st_dev,
            current.st_ino,
            current.st_size,
        ):
            raise OSError("artifact path changed while being read")
    finally:
        os.close(check_fd)
        for directory in reversed(check_directories):
            os.close(directory)
    return data


def _looks_binary(data: bytes) -> bool:
    """True when the artifact is clearly not a text file (e.g. mp3/video).

    The judge cannot read binary payloads as text; binary artifacts are judged
    by their text summary (tool_result.stdout_preview) instead.
    """
    sample = data[:4096]
    if not sample:
        return False
    non_text = sum(1 for byte in sample if byte == 0 or byte > 126)
    return non_text / len(sample) > 0.3


# A phrase that introduces the answer the corrected script must now produce.
_FIX_RUN_TRIGGER_RE = re.compile(
    r"ожидает(?:ся|ось)|ожида(?:лось|емо|ется)|"
    r"долж\w*\s+(?:быть|вывест[иь]|выводить|напечатать|печатать|"
    r"возвраща(?:ть|ется)|вернуть|равнять?ся|получиться|стать|дать)|"
    r"правильн\w*\s+(?:ответ\w*|результат\w*|значени\w*|вывод\w*)|"
    r"верн\w*\s+(?:ответ\w*|результат\w*|значени\w*)|"
    r"в\s+(?:итоге|результате)|"
    r"expected(?:\s+(?:to\s+(?:be|print|return|output|equal)|"
    r"result|value|output))?|expect|"
    r"should\s+(?:be|print|return|output|equal|yield|contain)",
    re.IGNORECASE,
)
# A value literal: signed int/float, dotted version, or a known bool/none literal.
# Bare identifiers are intentionally NOT accepted — they cause false rejects far
# more often than they help, and the canonical fix-run expectation is a number.
_FIX_RUN_VALUE_RE = re.compile(
    r"[«\"'(\[]?\s*("
    r"-?\d+(?:[.,]\d+)?"
    r"|\d+(?:\.\d+){2,}"
    r"|[Tt]rue|[Ff]alse|[Nn]one|null"
    r")"
)


def _fix_run_expected_token(goal: str) -> str | None:
    """Best-effort requested rerun postcondition of a ``file_write_fix_run`` goal.

    Such a goal states the answer the corrected script must now produce (e.g.
    «пойми почему результат неверный (ожидается 15)», «результат должен
    равняться 15», «в итоге должна вывести 15»). The rerun stdout must actually
    carry that value before structural DONE is granted — a non-empty, hash-valid
    artifact with the *wrong* number must fail closed. Two-stage: find an
    expectation trigger, then take the first value literal within a short window
    after it. Returns ``None`` when the goal states no explicit expectation.
    """
    if not goal:
        return None
    trigger = _FIX_RUN_TRIGGER_RE.search(goal)
    if not trigger:
        return None
    window = goal[trigger.end() : trigger.end() + 64]
    value = _FIX_RUN_VALUE_RE.search(window)
    if not value:
        return None
    token = value.group(1).strip().strip(".,;:)»\"'")
    return token or None


# The materialized rerun artifact ends with the orchestrator-appended
# ``\n\nexit code:\n<n>\n`` trailer (orchestrator.py: ``_materialize_stdout_artifact``).
# That trailer is NOT program stdout, so the postcondition value search must not
# see it — otherwise a goal that expects ``0`` is spuriously satisfied by the
# ``exit code:\n0`` of any clean rerun (P1-01: false DONE for an unresolved fix).
_FIX_RUN_TRAILER_RE = re.compile(r"\n?exit[ _]?code[:\s]*\s*-?\d+\s*\Z", re.IGNORECASE)


def _fix_run_stdout_satisfies(stdout_text: str, token: str) -> bool:
    """The expected token must appear as a *standalone value* in the rerun
    stdout, not merely as a substring: expecting ``15`` must not be satisfied by
    ``150`` or ``1523``, and expecting ``5`` must not be satisfied by ``-5``.
    The trailing ``exit code:`` section is stripped first so the exit-code value
    can never stand in for the requested program output.
    """
    body = _FIX_RUN_TRAILER_RE.sub("", stdout_text)
    pattern = re.compile(r"(?<![\w.\-])" + re.escape(token) + r"(?![\w.\-])")
    return pattern.search(body) is not None


_FIX_RUN_EXITCODE_RE = re.compile(
    r"exit[ _]?code[:\s]*\s*(-?\d+)", re.IGNORECASE
)


def _fix_run_exit_code(stdout_text: str) -> int | None:
    """Parse the ``exit code:\\n<n>`` trailer of a materialized run-stdout
    artifact. The trailer is always appended last, so the *last* match wins —
    a script that itself prints the words "exit code" does not shadow it.
    ``None`` when the artifact carries no recognizable exit section.
    """
    matches = _FIX_RUN_EXITCODE_RE.findall(stdout_text)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def _tool_result_preview(task: TaskFlow) -> str:
    """Text summary of the executed tool (stdout_preview), for binary artifacts."""
    try:
        step = task.steps[0]
        output = step.output or {}
        projection = output.get("tool_result") or {}
        preview = projection.get("stdout_preview")
        return str(preview) if isinstance(preview, str) else ""
    except Exception:
        return ""


def create_verifier_app(
    database_url: str | None = None,
    credential: str | None = None,
    judge: LLMJudge | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    resolved_db_url = database_url or os.getenv("ANTIGONA_DATABASE_URL") or "sqlite:///./antigona.db"
    db = Database(resolved_db_url)
    criteria_db = VerifierCriteriaDatabase(resolved_db_url)
    secret = credential or verifier_credential()
    settings = settings or Settings.from_env()
    result_channels = list(
        dict.fromkeys(
            channel.strip().lower()
            for channel in (settings.delivery_result_channels or ["telegram"])
            if channel and channel.strip()
        )
    ) or ["telegram"]
    if judge is None:
        api_key = os.getenv("ANTIGONA_OPENROUTER_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
        test_mode = settings.test_mode or os.getenv("ANTIGONA_TEST_MODE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if api_key and not test_mode:
            provider = HTTPVerifierProvider(
                os.getenv("ANTIGONA_LLM_API_URL", "https://openrouter.ai/api/v1/chat/completions"),
                api_key,
            )
            verifier_judge = LLMJudge(
                primary_model=settings.model_primary,
                verifier_model=settings.model_secondary,
                provider=provider,
            )
        elif credential or test_mode:
            # Test mode: credential provided or test_mode enabled — create a mock judge
            # that is never hit by /skills/{id}/promote
            from .verifier.judge import ProviderResult

            class _MockJudge:
                model_name = "mock-verifier"

                def evaluate(self, *args: Any, **kwargs: Any) -> ProviderResult:
                    return ProviderResult(
                        approved=True, reason="mock pass", actual_model="mock-verifier"
                    )

            verifier_judge = _MockJudge()  # type: ignore[assignment]
        else:
            raise RuntimeError("independent verifier provider credential is required")
    else:
        verifier_judge = judge

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db.create_all()
        criteria_db.create_all()
        yield

    app = FastAPI(title="Antigona Verifier v2", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "service": "verifier"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness: DB reachable => ready, else 503 (fail-closed)."""
        try:
            with db.session_factory() as session:
                session.execute(select(1))
            return JSONResponse(
                status_code=200,
                content={"status": "ready", "service": "verifier", "database": "ok"},
            )
        except Exception as exc:  # noqa: BLE001 — readiness must fail closed
            logger.exception("verifier readiness check failed")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "service": "verifier",
                    "database": "error",
                    "error": type(exc).__name__,
                },
            )

    def emit_outcome(
        name: str,
        body: VerifyRequest,
        status: str,
        task: TaskFlow | None = None,
    ) -> None:
        event(
            name,
            service="verifier",
            correlation_id=body.correlation_id,
            task_id=body.task_id,
            session_id=task.owner_id if task is not None else None,
            step_id=None,
            status=status,
        )

    def reject(
        session: Session, task: TaskFlow, body: VerifyRequest, reason: str
    ) -> dict[str, str]:
        safe_reason = sanitize_failure_reason(reason) or "verification rejected"
        repository = TaskRepository(session)
        try:
            repository.transition(
                task,
                TaskState.FAILED,
                reason=f"Verifier rejected: {safe_reason}",
                actor="verifier-service",
                correlation_id=body.correlation_id,
            )
            repository.commit()
        except InvalidTransition:
            # The task already reached a terminal state (e.g. the worker's
            # watchdog or a concurrent verify call marked it FAILED first).
            # Nothing to reconcile — return the rejection verdict without
            # turning the endpoint into a 500 that the worker misreads as
            # "verification service unavailable".
            session.rollback()
            session.expire_all()
            return {"decision": "REPLAN"}
        emit_outcome("verifier.rejected", body, "rejected", task)
        return {"decision": "REPLAN"}

    @app.post("/verify")
    def verify(
        body: VerifyRequest, authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {secret}"):
            emit_outcome("verifier.authentication_failed", body, "denied")
            raise HTTPException(401, "verifier credential required")

        with db.session_factory() as session:
            task = session.scalar(select(TaskFlow).where(TaskFlow.id == body.task_id))
            if not task:
                emit_outcome("verifier.task_missing", body, "not_found")
                raise HTTPException(404, "task not found")
            if task.status != TaskState.VERIFYING.value:
                emit_outcome("verifier.invalid_state", body, "denied", task)
                raise HTTPException(409, "task is not VERIFYING")

            artifact = session.scalar(
                select(Artifact)
                .outerjoin(FlowStep, Artifact.step_id == FlowStep.id)
                .where(Artifact.task_id == task.id)
                .order_by(FlowStep.index.desc().nulls_last(), Artifact.created_at.desc())
                .limit(1)
            )
            if not artifact:
                emit_outcome("verifier.artifact_missing", body, "rejected", task)
                raise HTTPException(409, "artifact missing")

            from .core import paths

            # The configured workspace wins without any pathname inspection
            # (the descriptor reader must be the first artifact read); the
            # governed resolver is only consulted when unset.
            _ws_env = os.environ.get("ANTIGONA_WORKSPACE")
            workspace = Path(_ws_env) if _ws_env else paths.workspace_dir()
            # The descriptor reader is the first and only artifact-content read.
            try:
                data = read_artifact_safely(workspace, artifact.path, artifact.size)
            except OSError:
                reject(session, task, body, "artifact unreadable")
                raise HTTPException(409, "artifact verification failed") from None

            # 1. Artifact content-hash verification over the descriptor bytes.
            computed_sha = hashlib.sha256(data).hexdigest()
            if computed_sha != artifact.sha256:
                return reject(session, task, body, "artifact hash mismatch")

            # 1b. D10 fail-closed: literal path constraints from the goal must
            # be satisfied by the verified artifact. A read/write of the WRONG
            # file (e.g. default task_output.txt when the goal names
            # literal.txt) must never finalize as DONE — otherwise a fully
            # unperformed task reports success. Applies to file tools only
            # (shell stdout/email/mcp artifacts have no goal-named path).
            _is_file_task = task.tool_name in (
                "workspace.write_text",
                "workspace.read_text",
            ) or any(
                s.tool_name in ("workspace.write_text", "workspace.read_text")
                or (
                    s.input
                    and s.input.get("tool_name")
                    in ("workspace.write_text", "workspace.read_text")
                )
                for s in task.steps
            )
            if _is_file_task:
                from antigona.task_goal import expected_paths_from_goal

                _expected = expected_paths_from_goal(task.goal or "")
                if _expected:
                    _art_path = str(artifact.path or "").replace("\\", "/").rstrip("/")
                    # The compound plan's own target (e.g. folder/summary.txt)
                    # is a legitimate artifact even though it is not one of the
                    # expected per-file paths.
                    from antigona.task_goal import parse_goal as _pg
                    _plan_target = str(getattr(_pg(task.goal or ""), "path", "") or "").replace("\\", "/").rstrip("/")
                    # H2: compare on segment boundaries — "x/a.txt" must not
                    # match expected "a.txt" via a loose endswith (a single
                    # file goal must not be satisfied by a file in a
                    # subfolder). A stdout plan target is not a file path.
                    def _segments_match(_candidate: str, _target: str) -> bool:
                        # H2: exact path equality only — a prefixed artifact
                        # (x/evidence/a.txt) must never satisfy goal path
                        # (evidence/a.txt).
                        return _candidate == _target.lstrip("/")

                    if _plan_target in ("", "stdout"):
                        _plan_target = ""
                    # B5/L7-1: a compound write→run / write→run→fix→run goal ends with the RUN step, whose
                    # artifact is the materialized stdout (.antigona-results/…),
                    # not the created script. The script itself is still enforced
                    # by the compound required-files check below.
                    _intent = str(getattr(_pg(task.goal or ""), "intent", ""))
                    _is_run_stdout = (
                        _intent in ("file_write_run", "file_write_fix_run")
                        and _art_path.startswith(".antigona-results/")
                    )
                    if _is_run_stdout:
                        pass
                    elif _intent == "file_write_fix_run":
                        return reject(
                            session,
                            task,
                            body,
                            "fix-run compound requires rerun stdout artifact, write-only artifact rejected",
                        )
                    _is_summary_target = (
                        _intent == "multi_file"
                        and bool(_plan_target)
                        and _plan_target.endswith("summary.txt")
                        and _segments_match(_art_path, _plan_target)
                    )
                    if (
                        not _is_run_stdout
                        and not any(
                            _segments_match(_art_path, expected)
                            for expected in _expected
                        )
                        and not _is_summary_target
                    ):
                        return reject(
                            session,
                            task,
                            body,
                            "artifact path does not match goal",
                        )
                                    # BAM-6 invariant: compound goals (multi_file /
            # file_write_read intents) declare MULTIPLE required file
            # operations. DONE is forbidden unless EVERY required path
            # exists as a real regular file inside the workspace —
            # otherwise a partially-performed task reports success.
            # Applied to ALL tools (file AND shell/multi_file paths).
            import stat as _stat

            from antigona.task_goal import _extract_multi_file, parse_goal

            _plan = parse_goal(task.goal or "")
            if getattr(_plan, "incomplete_sequence", False) or getattr(_plan, "note", "") == "incomplete_sequence":
                emit_outcome(
                    "verifier.incomplete_sequence",
                    body,
                    "rejected",
                    task,
                )
                return reject(
                    session,
                    task,
                    body,
                    "compound goal incomplete: subsequent sequence operation not planned",
                )

            _is_file_write_task = (
                task.tool_name == "workspace.write_text"
                or _plan.intent in ("file_write", "multi_file", "file_write_read", "file_write_run", "file_write_fix_run")
                or any(
                    s.tool_name == "workspace.write_text"
                    or (s.input and s.input.get("tool_name") == "workspace.write_text")
                    for s in task.steps
                )
            )

            if _is_file_write_task:
                _ws_resolved = workspace.resolve()
                # Required paths mirror what the executor actually WRITES:
                #  - multi_file: folder-prefixed targets (same as
                #    _build_multi_file_command) plus the summary target for
                #    value plans;
                #  - file_write_read / file_write_run / file_write_fix_run: every WORKSPACE-RELATIVE expected path;
                #  - file_write: all goal-expected relative paths (or the relative target/artifact path).
                _required: list[str] = []
                if _plan.intent == "multi_file":
                    _mf = _extract_multi_file(task.goal or "") or {}
                    _folder = str(_mf.get("folder") or "").strip("/")
                    for _name in _plan.expected_paths:
                        # External read mentions (absolute paths such as
                        # /etc/hostname) are not workspace write targets.
                        if _name.startswith("/"):
                            continue
                        _required.append(
                            f"{_folder}/{_name}".strip("/")
                            if _folder and "/" not in _name
                            else _name
                        )
                    # summary is a required artifact only for value plans
                    # (structure-only plans just touch files + list them).
                    if _plan.path and _plan.path != "stdout" and _plan.content:
                        _required.append(_plan.path.strip("/"))
                elif _plan.intent in ("file_write_read", "file_write_run", "file_write_fix_run"):
                    _required = [
                        _p
                        for _p in _plan.expected_paths
                        if not _p.lstrip("/").startswith("/")
                        and not _p.startswith("/")
                        and _p != "/"
                    ]
                else:
                    if _plan.expected_paths:
                        _required = [
                            _p
                            for _p in _plan.expected_paths
                            if not _p.lstrip("/").startswith("/")
                            and not _p.startswith("/")
                            and _p != "/"
                        ]
                    elif artifact.path and not str(artifact.path).startswith("/"):
                        _required = [str(artifact.path)]

                _missing: list[str] = []
                for _req_path in _required:
                    _raw = _ws_resolved.joinpath(_req_path.lstrip("/"))
                    # Component-wise symlink rejection BEFORE resolve: a
                    # symlinked parent directory must not smuggle a file into
                    # the workspace (anti reward-hacking).
                    try:
                        _symlink_component = any(
                            _part.is_symlink()
                            for _part in (_raw, *_raw.parents)
                            if _part != _ws_resolved and _ws_resolved in _part.parents
                        )
                        if _raw.is_symlink() or _symlink_component:
                            _missing.append(_req_path)
                            continue
                        _resolved = _raw.resolve()
                        if not _resolved.is_relative_to(_ws_resolved):
                            _missing.append(_req_path)
                            continue
                        if not _resolved.exists():
                            _missing.append(_req_path)
                            continue
                        _st = _resolved.stat()
                        if not _stat.S_ISREG(_st.st_mode):
                            _missing.append(_req_path)
                            continue
                        # H1: a hardlink (nlink>1) is not a fresh written file —
                        # an attacker could ln existing content to the target.
                        if _st.st_nlink > 1:
                            _missing.append(_req_path)
                            continue
                        # Structure-only plans may legitimately produce empty
                        # files (touch); value plans must be non-empty.
                        # B5: a script that must be RUN is never legitimately
                        # empty and must never carry markdown fences (```python)
                        # — `python x.py` would die with SyntaxError.
                        if (_plan.content or _plan.intent in ("file_write_run", "file_write_fix_run")) and _st.st_size == 0:
                            _missing.append(_req_path)
                            continue
                        if _plan.intent in ("file_write_run", "file_write_fix_run"):
                            try:
                                _head = _resolved.read_text(
                                    encoding="utf-8", errors="replace"
                                ).lstrip()
                            except OSError:
                                _missing.append(_req_path)
                                continue
                            if _head.startswith("```"):
                                return reject(
                                    session,
                                    task,
                                    body,
                                    "created script contains markdown code fences "
                                    f"and is not executable: {_req_path}",
                                )
                    except OSError:
                        _missing.append(_req_path)
                if _missing:
                    emit_outcome(
                        "verifier.required_files_missing",
                        body,
                        "rejected",
                        task,
                    )
                    return reject(
                        session,
                        task,
                        body,
                        f"required file(s) missing: {', '.join(_missing)}",
                    )

            # 2. Trajectory checks consume only the already-read bytes/hash.
            anomalies = detect_trajectory_anomalies(
                session,
                task,
                artifact_path=artifact.path,
                artifact_bytes=data,
                artifact_sha256=computed_sha,
                artifact_id=artifact.id,
            )
            if anomalies:
                finding = anomalies[0]
                return reject(
                    session,
                    task,
                    body,
                    f"trajectory:{finding.code}:{finding.detail}",
                )

            # 3. Structural evidence is mandatory regardless of model approval.
            actual_text = data.decode(errors="replace")
            if _looks_binary(data):
                # Binary artifacts (e.g. TTS mp3) cannot be judged as text —
                # fall back to the tool's text summary (stdout_preview).
                preview = _tool_result_preview(task)
                if preview:
                    actual_text = preview
            is_read_tool = task.tool_name == "workspace.read_text" or any(
                s.tool_name == "workspace.read_text"
                or (s.input and s.input.get("tool_name") == "workspace.read_text")
                for s in task.steps
            )
            # B5: the final artifact of a write→run compound is the
            # materialized run stdout (.antigona-results/<task>.txt), whose
            # "stdout:\n…\n\nexit code:\n0" layout carries meaning in its line
            # breaks. Collapsing them would deliver "stdout:144.0exit code:0",
            # which canon forbids. Plain shell stdout-only tasks are unchanged.
            from antigona.task_goal import parse_goal as _pg_result

            _goal_intent = str(getattr(_pg_result(task.goal or ""), "intent", ""))
            _is_fix_run_stdout = (
                _goal_intent == "file_write_fix_run"
                and str(artifact.path or "").replace("\\", "/").startswith(".antigona-results/")
            )
            _run_stdout_artifact = (
                _goal_intent in ("file_write_run", "file_write_fix_run")
                and str(artifact.path or "").replace("\\", "/").startswith(".antigona-results/")
            )
            safe_actual_text = sanitize_result_text(
                actual_text,
                preserve_newlines=is_read_tool or _run_stdout_artifact,
            )
            if not isinstance(safe_actual_text, str) or not is_usable_result_text(safe_actual_text):
                return reject(session, task, body, "artifact contains no usable result text")

            # R1-B01: a fix-run's contract is that the *rerun* now works and
            # prints the requested answer. Structural evidence (hash + non-empty)
            # is not sufficient. Two fail-closed postconditions before DONE:
            #   1. if the artifact carries an exit-code trailer, it must be 0 —
            #      a rerun that still errors has not resolved the defect (this
            #      also covers a traceback whose line number coincides with the
            #      expected value);
            #   2. if the goal states an explicit expected result, that value
            #      must appear in the rerun stdout as a standalone token, not
            #      merely as a substring (15 is not satisfied by 150 or -15).
            # A goal with no derivable expectation keeps the prior structural
            # behaviour (documented residual: no stated ground truth to check).
            if _is_fix_run_stdout:
                _exit_code = _fix_run_exit_code(actual_text)
                if _exit_code is not None and _exit_code != 0:
                    return reject(
                        session,
                        task,
                        body,
                        "fix-run rerun exited non-zero "
                        f"(exit code {_exit_code}); defect not resolved",
                    )
                _expected_token = _fix_run_expected_token(task.goal or "")
                if _expected_token and not _fix_run_stdout_satisfies(
                    actual_text, _expected_token
                ):
                    return reject(
                        session,
                        task,
                        body,
                        "fix-run rerun stdout does not satisfy requested "
                        f"postcondition (expected {_expected_token!r} as a "
                        "standalone value)",
                    )

            # P0-031 FALSE_DONE guard: если из goal формально вычисляется точное
            # содержимое файла — фактический артефакт ОБЯЗАН совпасть с ним.
            # Размер файла == len(content) недостаточен: wrong content той же
            # длины (P0-031: «двумя строками: FIRST и SECOND» вместо
            # «FIRST\nSECOND») никогда не получает DONE.
            expected_content = deterministic_expected_content(task.goal or "")
            if expected_content:
                # Нормализация: goal-литерал может быть обёрнут в кавычки
                # («с текстом "Version 1.2 released"»), а файл содержит голый
                # текст. Сравниваем по сути, не по обрамлению.
                def _norm(v: str) -> str:
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
                        v = v[1:-1]
                    return v.rstrip("\r\n")

                content_matches = False
                try:
                    exp_json = json.loads(expected_content)
                    act_json = json.loads(actual_text)
                    content_matches = (exp_json == act_json)
                except Exception:
                    content_matches = (_norm(actual_text) == _norm(expected_content))

                if not content_matches:
                    return reject(
                        session,
                        task,
                        body,
                        "artifact content does not match deterministic goal content",
                    )

            # 3b. BUG ANT-002 P2 / wave3 smoke: read-tasks AND deterministic
            # write-tasks (explicit content) have ground-truth the LLM judge
            # cannot verify better than structural evidence — it guesses and
            # rejects correct short artifacts ("only content 'smoke ok' but…").
            # Structural evidence (file exists, hash matches, non-empty text)
            # IS the verifiable contract; finalize DONE without the subjective
            # secondary model.
            # Wave 4: structural write-verification applies only to real LLM
            # judges. Test judges (DeterministicVerifierProvider, model_name
            # "test-*") must run the judge path so explicit approve/deny
            # decisions (approved=False -> REPLAN) keep working.
            is_test_judge = str(getattr(verifier_judge, "model_name", "")).startswith("test-")
            # Wave 0 (CC-02): structural DONE for a write artifact requires
            # exact UTF-8 byte equality against task.content, not merely equal
            # SIZE or normalized decoded text. Byte-distinct content must fall
            # through to the judge path, never DONE.
            write_content_matches = False
            if task.content:
                try:
                    write_content_matches = data == task.content.encode("utf-8")
                except UnicodeEncodeError:
                    write_content_matches = False
            is_structural = is_read_tool or (
                not is_test_judge
                and (
                    _is_fix_run_stdout
                    or write_content_matches
                )
            )
            if is_structural:
                _safe_read_reason = (
                    "fix-run rerun stdout verified structurally (hash+non-empty)"
                    if _is_fix_run_stdout
                    else "read artifact verified structurally (hash+non-empty)"
                )
                result = session.execute(
                    update(TaskFlow)
                    .where(
                        TaskFlow.id == task.id,
                        TaskFlow.status == TaskState.VERIFYING.value,
                        TaskFlow.revision == task.revision,
                    )
                    .values(
                        status=TaskState.DONE.value,
                        revision=task.revision + 1,
                        updated_at=utcnow(),
                    )
                )
                assert isinstance(result, CursorResult)
                if result.rowcount != 1:
                    session.rollback()
                    emit_outcome("verifier.cas_failed", body, "error", task)
                    raise HTTPException(409, "finalization CAS failed")
                artifact.verified = True
                artifact.evidence = {
                    "sha256": artifact.sha256,
                    "read_back": safe_actual_text,
                    "judge_model": "structural",
                    "judge_reason": _safe_read_reason,
                    "judge_actual_model": "structural",
                }
                # Wave 4 (B): the structural path must fan out the result to the
                # same delivery channels as the judge path — otherwise DONE
                # tasks never produce a "result" outbox event.
                if is_read_tool:
                    read_step = next(
                        (
                            s
                            for s in reversed(task.steps)
                            if s.tool_name == "workspace.read_text"
                            or (s.input and s.input.get("tool_name") == "workspace.read_text")
                        ),
                        None,
                    ) or (task.steps[0] if task.steps else None)
                    step_output = (read_step.output or {}) if read_step else {}
                    proj = step_output.get("tool_result") or {}
                    path_val = proj.get("path") or sanitize_result_text(artifact.path, escape_html=False) or ""
                    tool_name_val = proj.get("tool_name") or "workspace.read_text"
                    creator_tool_val = proj.get("creator_tool") or (
                        "workspace.write_text"
                        if any(
                            s.tool_name == "workspace.write_text"
                            or (s.input and s.input.get("tool_name") == "workspace.write_text")
                            for s in task.steps
                        )
                        else None
                    )
                    msg_parts = [f"path: {path_val}", f"tool_name: {tool_name_val}"]
                    if creator_tool_val:
                        msg_parts.append(f"creator_tool: {creator_tool_val}")
                    msg_parts.append(f"content:\n{safe_actual_text}")
                    delivery_message = "\n".join(msg_parts)
                else:
                    delivery_message = safe_actual_text
                for channel in result_channels:
                    session.add(
                        DeliveryOutbox(
                            task_id=task.id,
                            adapter=channel,
                            event_type="result",
                            idempotency_key=f"result:{task.id}:{channel}",
                            payload={
                                "task_id": task.id,
                                "session_id": task.owner_id,
                                "correlation_id": body.correlation_id,
                                "step_id": None,
                                "status": TaskState.DONE.value,
                                "message": delivery_message,
                            },
                        )
                    )
                # Wave 4: the structural path must also record the DONE
                # transition (AGENTS.md invariant: verifier-service owns DONE).
                transition = StateTransition(
                    task_id=task.id,
                    entity_id=task.id,
                    entity_type="task",
                    from_state=TaskState.VERIFYING.value,
                    to_state=TaskState.DONE.value,
                    reason=_safe_read_reason,
                    actor="verifier-service",
                    correlation_id=body.correlation_id,
                )
                session.add(transition)
                session.commit()
                emit_outcome("verifier.approved", body, "approved", task)
                event(
                    "verifier.completed",
                    service="verifier",
                    correlation_id=body.correlation_id,
                    task_id=task.id,
                    session_id=task.owner_id,
                    step_id=None,
                    status=TaskState.DONE.value,
                )
                return {"decision": "DONE"}

            # 4. Secondary-model evaluation receives only safe text.
            try:
                with criteria_db.session_factory() as criteria_session:
                    criteria = VerifierCriteriaStore(criteria_session).require(task.id)
                verdict = verifier_judge.evaluate(
                    goal=task.goal,
                    criteria=criteria,
                    actual_content=safe_actual_text,
                    evidence={"sha256": artifact.sha256},
                )
            except MissingVerifierCriteria:
                return reject(session, task, body, "private verifier criteria missing")
            except (
                ProviderTransportError,
                ProviderMalformedResponse,
                ProviderModelMismatch,
            ):
                return reject(session, task, body, "verification provider unavailable")
            except Exception:
                return reject(session, task, body, "verification provider unavailable")
            if not verdict.approved:
                return reject(session, task, body, f"judge rejected: {verdict.reason}")

            safe_judge_reason = sanitize_failure_reason(verdict.reason) or "verification passed"

            # 5. Finalization CAS: VERIFYING -> DONE (Verifier-only capability)
            result = session.execute(
                update(TaskFlow)
                .where(
                    TaskFlow.id == task.id,
                    TaskFlow.status == TaskState.VERIFYING.value,
                    TaskFlow.revision == task.revision,
                )
                .values(
                    status=TaskState.DONE.value,
                    revision=task.revision + 1,
                    updated_at=utcnow(),
                )
            )
            assert isinstance(result, CursorResult)
            if result.rowcount != 1:
                emit_outcome("verifier.cas_failed", body, "error", task)
                raise HTTPException(409, "finalization CAS failed")

            artifact.verified = True
            artifact.evidence = {
                "sha256": artifact.sha256,
                "read_back": safe_actual_text,
                "judge_model": verifier_judge.model_name,
                "judge_reason": safe_judge_reason,
                "judge_actual_model": verdict.actual_model,
            }

            correlation = body.correlation_id
            transition = StateTransition(
                task_id=task.id,
                entity_id=task.id,
                entity_type="task",
                from_state=TaskState.VERIFYING.value,
                to_state=TaskState.DONE.value,
                reason=f"Verifier v2 ({verifier_judge.model_name}) passed: {safe_judge_reason}",
                actor="verifier-service",
                correlation_id=correlation,
            )
            session.add(transition)
            session.flush()

            # Fan out the sanitized verified result to every configured result
            # channel, in the same transaction as the DONE CAS above. The
            # idempotency key is deterministic per (task, channel), and the
            # CAS above already guarantees this code path runs at most once
            # per task, so a retried /verify request never duplicates rows.
            delivery_message = safe_actual_text
            if is_read_tool:
                read_step = next(
                    (
                        s
                        for s in reversed(task.steps)
                        if s.tool_name == "workspace.read_text"
                        or (s.input and s.input.get("tool_name") == "workspace.read_text")
                    ),
                    None,
                ) or (task.steps[0] if task.steps else None)
                step_output = (read_step.output or {}) if read_step else {}
                proj = step_output.get("tool_result") or {}
                path_val = (
                    proj.get("path")
                    or sanitize_result_text(artifact.path, escape_html=False)
                    or ""
                )
                tool_name_val = (
                    proj.get("tool_name")
                    or ("workspace.read_text" if is_read_tool else sanitize_result_text(task.tool_name, escape_html=False))
                    or ""
                )
                creator_tool_val = proj.get("creator_tool") or (
                    "workspace.write_text"
                    if any(
                        s.tool_name == "workspace.write_text"
                        or (s.input and s.input.get("tool_name") == "workspace.write_text")
                        for s in task.steps
                    )
                    else None
                )

                msg_parts = [
                    f"path: {path_val}",
                    f"tool_name: {tool_name_val}",
                ]
                if creator_tool_val:
                    msg_parts.append(f"creator_tool: {creator_tool_val}")
                msg_parts.append(f"content:\n{safe_actual_text}")
                delivery_message = "\n".join(msg_parts)

            for channel in result_channels:
                session.add(
                    DeliveryOutbox(
                        task_id=task.id,
                        adapter=channel,
                        event_type="result",
                        idempotency_key=f"result:{task.id}:{channel}",
                        payload={
                            "task_id": task.id,
                            "session_id": task.owner_id,
                            "correlation_id": correlation,
                            "step_id": None,
                            "status": TaskState.DONE.value,
                            "message": delivery_message,
                        },
                    )
                )
            session.commit()
            event(
                "verifier.completed",
                service="verifier",
                correlation_id=correlation,
                task_id=task.id,
                session_id=task.owner_id,
                step_id=None,
                status=TaskState.DONE.value,
            )
            return {"decision": "DONE"}

    @app.post("/skills/{skill_id}/promote")
    def promote_skill(
        skill_id: str,
        body: PromoteSkillRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {secret}"):
            raise HTTPException(401, "verifier credential required")

        state_root = os.environ.get("ANTIGONA_STATE_ROOT", "/var/lib/antigona")

        with db.session_factory() as session:
            skill = session.scalar(select(Skill).where(Skill.id == skill_id))
            if not skill:
                raise HTTPException(404, f"skill {skill_id} not found")

            if skill.revision != body.revision:
                raise HTTPException(
                    409,
                    f"skill revision mismatch: expected {skill.revision}, got {body.revision}",
                )

            if skill.status != SkillState.CANDIDATE.value:
                raise HTTPException(409, f"skill status is {skill.status}, required CANDIDATE")

            registry = SkillsRegistry(session)

            try:
                card_body = registry.get_card_body(skill, state_root)
                verify_skill_card_for_promotion(
                    session,
                    card_body,
                    skill.body_sha256,
                    skill.source_flow_id,
                )
            except (SkillVerificationError, SkillIntegrityError, OSError) as exc:
                registry.transition_to(
                    skill,
                    SkillState.REJECTED.value,
                    actor="verifier-service",
                    reason=f"promotion check failed: {exc}",
                    correlation_id=body.correlation_id,
                )
                session.commit()
                raise HTTPException(409, f"skill verification failed: {exc}") from exc

            promoted = registry.promote(
                skill_id,
                verifier_actor="verifier-service",
                reason="promoted by verifier service",
                correlation_id=body.correlation_id,
            )
            return {
                "decision": "ACTIVE",
                "skill_id": promoted.id,
                "revision": promoted.revision,
                "status": promoted.status,
            }

    return app


def main() -> None:
    import uvicorn

    from .health.heartbeat import HeartbeatReporter
    from .security import verifier_credential

    try:
        cred = verifier_credential()
    except (KeyError, ValueError):
        cred = None

    # Liveness heartbeat so the Gateway's /health reports this service as "ok"
    # instead of "неизвестно (нет heartbeat)" (same pattern as worker/delivery).
    verifier_heartbeat = HeartbeatReporter("verifier")
    verifier_heartbeat.start()
    try:
        uvicorn.run(
            create_verifier_app(credential=cred),
            host=os.getenv("ANTIGONA_VERIFIER_HOST", "127.0.0.1"),
            port=int(os.getenv("ANTIGONA_VERIFIER_PORT", "8091")),
        )
    finally:
        verifier_heartbeat.stop()


if __name__ == "__main__":
    main()
