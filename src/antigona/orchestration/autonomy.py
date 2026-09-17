"""Fail-closed autonomy evidence and filesystem boundaries for M2 stages."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from antigona.core import paths

# HOME inside the bubblewrap sandbox. The real /home and /root are tmpfs'd away,
# so every credential bind target is derived from this single prefix.
SANDBOX_HOME = "/tmp/home"


class AutonomyContractError(RuntimeError):
    """A stage did not produce the world-bound evidence its contract requires."""


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ignored(relative: PurePosixPath) -> bool:
    return bool(relative.parts and relative.parts[0] in {".git", ".antigona"})


def _protected(relative: PurePosixPath) -> bool:
    name = relative.name.lower()
    return (
        any(part.lower() in {"test", "tests"} for part in relative.parts[:-1])
        or bool(relative.parts and relative.parts[0].lower() == ".github")
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name in {
            "cargo.lock", "cargo.toml", "conftest.py", "dockerfile", "go.mod", "go.sum",
            "makefile", "mypy.ini", "package-lock.json", "package.json", "poetry.lock",
            "pyproject.toml", "pytest.ini", "setup.cfg", "setup.py", "tox.ini", "uv.lock",
        }
        or name.startswith("requirements")
        or name.endswith((".lock", ".cfg", ".ini"))
    )


def _production_source(relative: PurePosixPath) -> bool:
    return relative.suffix.lower() in {
        ".c", ".cc", ".cpp", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
        ".php", ".py", ".rb", ".rs", ".sh", ".ts", ".tsx",
    }


@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace: str
    tree_hash: str
    files: dict[str, str]
    protected_files: dict[str, str]
    production_files: dict[str, str]


def snapshot_workspace(workspace: Path) -> WorkspaceSnapshot:
    root = WorkspaceBoundary(workspace, writable=False).validate()
    files: dict[str, str] = {}
    protected: dict[str, str] = {}
    production: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _ignored(relative):
            continue
        if path.is_symlink():
            raise AutonomyContractError(f"symlink in workspace is forbidden: {relative}")
        if not path.is_file():
            continue
        digest = hash_file(path)
        key = relative.as_posix()
        files[key] = digest
        if _protected(relative):
            protected[key] = digest
        elif _production_source(relative):
            production[key] = digest
    return WorkspaceSnapshot(
        workspace=str(root),
        tree_hash=_json_hash(files),
        files=files,
        protected_files=protected,
        production_files=production,
    )


@dataclass(frozen=True)
class BoundaryResult:
    returncode: int
    stdout: str
    stderr: str


class WorkspaceBoundary:
    """Run argv with the host root read-only and only one optional writable bind."""

    def __init__(self, workspace: Path, *, writable: bool) -> None:
        self.workspace = workspace
        self.writable = writable

    def validate(self) -> Path:
        if ".." in self.workspace.parts:
            raise AutonomyContractError("workspace traversal is forbidden")
        lexical = self.workspace.absolute()
        if lexical == Path("/") or not lexical.is_dir():
            raise AutonomyContractError("workspace must be an existing non-root directory")
        current = Path(lexical.anchor)
        for part in lexical.parts[1:]:
            current /= part
            if current.is_symlink():
                raise AutonomyContractError(f"symlink path component forbidden: {current}")
        resolved = lexical.resolve(strict=True)
        code_root = Path(__file__).resolve().parents[3]
        home = paths.home_dir()
        protected_roots = (
            code_root,
            Path("/home"),
            home / ".agents",
            home / ".antigona",
            home / ".aws",
            home / ".codex",
            home / ".config",
            home / ".gnupg",
            home / ".hermes",
            home / ".local",
            home / ".ssh",
        )
        if resolved == home or any(
            resolved.is_relative_to(root.resolve(strict=False)) for root in protected_roots
        ):
            raise AutonomyContractError("workspace is a protected live or evidence path")
        for path in resolved.rglob("*"):
            if path.is_symlink():
                raise AutonomyContractError(f"symlink in workspace is forbidden: {path}")
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise AutonomyContractError(f"hardlinked workspace file is forbidden: {path}")
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise AutonomyContractError(f"special workspace file is forbidden: {path}")
        return resolved

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        extra_env: dict[str, str] | None = None,
    ) -> BoundaryResult:
        root = self.validate()
        home = paths.home_dir()
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise AutonomyContractError("bubblewrap unavailable; refusing unconfined execution")
        if not argv:
            raise AutonomyContractError("sandbox command is empty")
        confined_argv = [str(item) for item in argv]
        executable = shutil.which(confined_argv[0])
        private_executable: list[str] = []
        runtime_env: dict[str, str] = {}
        if Path(confined_argv[0]).name == "pytest":
            python_root = Path(sys.base_prefix).resolve()
            python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
            site_packages = next(
                path for path in map(Path, sys.path)
                if path.name == "site-packages" and path.is_dir()
            ).resolve()
            # Site-packages is mounted at its own mountpoint rather than nested
            # under the read-only interpreter bind. A nested target like
            # /tmp/antigona-python/lib/pythonX.Y/site-packages only exists when
            # the base interpreter layout happens to contain it (Ubuntu's /usr
            # python has dist-packages, not site-packages), and bubblewrap then
            # fails creating the mountpoint inside a read-only bind:
            # "Can't mkdir ...: Read-only file system". A separate --dir
            # mountpoint lives in the writable /tmp tmpfs and works with any
            # interpreter layout (system /usr python, setup-python, venvs).
            private_executable = [
                "--dir", "/tmp/antigona-python",
                "--ro-bind", str(python_root), "/tmp/antigona-python",
                "--dir", "/tmp/antigona-site",
                "--ro-bind", str(site_packages), "/tmp/antigona-site",
            ]
            confined_argv = [
                f"/tmp/antigona-python/bin/{python_version}", "-m", "pytest",
                *confined_argv[1:],
            ]
            runtime_env["PYTHONPATH"] = "/tmp/antigona-site"
        elif executable is not None and Path(executable).resolve().is_relative_to(home):
            source = Path(executable).resolve()
            target = f"/tmp/antigona-bin/{source.name}"
            private_executable = [
                "--dir", "/tmp/antigona-bin", "--ro-bind", str(source), target
            ]
            confined_argv[0] = target
        mount = "--bind" if self.writable else "--ro-bind"
        command = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--dir", SANDBOX_HOME,
            "--tmpfs", str(home),
            "--tmpfs", "/home",
            *private_executable,
        ]
        # Credential/tool binds are added ONLY when the source exists on this
        # machine: bubblewrap refuses to start ("Can't find source path") for a
        # missing bind source, and a normal host has neither ~/.local/bin nor
        # ~/.local/share/claude. The paths come from the canonical home helper so
        # no personal absolute path is hardcoded.
        for tool_source in (home / ".local" / "bin", home / ".local" / "share" / "claude"):
            if tool_source.is_dir():
                command.extend(["--ro-bind", str(tool_source), str(tool_source)])
        codex_dir = f"{SANDBOX_HOME}/.codex"
        codex_auth = home / ".codex" / "auth.json"
        codex_config = home / ".codex" / "config.toml"
        if codex_auth.is_file():
            command.extend(["--dir", codex_dir, "--ro-bind", str(codex_auth), f"{codex_dir}/auth.json"])
        if codex_config.is_file():
            if "--dir" not in command[-2:] and codex_dir not in command:
                command.extend(["--dir", codex_dir])
            command.extend(["--ro-bind", str(codex_config), f"{codex_dir}/config.toml"])

        grok_dir = f"{SANDBOX_HOME}/.grok"
        grok_auth = home / ".grok" / "auth.json"
        if grok_auth.is_file():
            command.extend(["--dir", grok_dir, "--ro-bind", str(grok_auth), f"{grok_dir}/auth.json"])

        claude_dir = f"{SANDBOX_HOME}/.claude"
        claude_creds = home / ".claude" / ".credentials.json"
        claude_settings = home / ".claude" / "settings.json"
        if claude_creds.is_file():
            command.extend(["--dir", claude_dir, "--ro-bind", str(claude_creds), f"{claude_dir}/.credentials.json"])
        if claude_settings.is_file():
            command.extend(["--ro-bind", str(claude_settings), f"{claude_dir}/settings.json"])

        # Antigravity (agy) keeps its OAuth session + config under ~/.gemini/antigravity-cli.
        # Bind it read-only into the sandbox home so 'agy -p' can authenticate in confined
        # (workspace) mode, mirroring the claude/codex/grok credential binds above.
        # Without this, confined agy exits 1: 'Error: authentication required' (host works
        # only because HOME exposes it) -> executor health UNAVAILABLE/AUTH_FAILURE.
        agy_home = home / ".gemini"
        agy_dir = f"{SANDBOX_HOME}/.gemini"
        if agy_home.is_dir():
            if "--dir" not in command[-2:] or agy_dir not in command:
                command.extend(["--dir", agy_dir])
            command.extend(["--ro-bind", str(agy_home), agy_dir])

        for i, arg in enumerate(confined_argv):
            if arg == "--prompt-file" and i + 1 < len(confined_argv):
                pf = Path(confined_argv[i + 1])
                if pf.is_file():
                    command.extend(["--ro-bind", str(pf), str(pf)])

        command.extend([
            "--dir", "/tmp/workspace",
            mount, str(root), "/tmp/workspace",
            "--chdir", "/tmp/workspace",
            "--",
            *confined_argv,
        ])
        env = {
            "HOME": SANDBOX_HOME,
            "LANG": "C.UTF-8",
            "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            **runtime_env,
        }
        allowed_extra = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
        if extra_env:
            env.update({key: value for key, value in extra_env.items() if key in allowed_extra})
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
                input="",  # codex exec otherwise waits for stdin in confined mode
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutonomyContractError(f"confined process failed: {exc}") from exc
        return BoundaryResult(completed.returncode, completed.stdout or "", completed.stderr or "")


@dataclass(frozen=True)
class FrozenCandidate:
    workspace: str
    baseline_hash: str
    candidate_hash: str
    protected_manifest_hash: str
    source_diff_hash: str
    archive_path: str
    implementation_run_id: str
    candidate_timestamp: str

    def with_hash(self, candidate_hash: str) -> FrozenCandidate:
        return replace(self, candidate_hash=candidate_hash)

    def as_dict(self) -> dict[str, str]:
        return {
            "workspace": self.workspace,
            "baseline_hash": self.baseline_hash,
            "candidate_hash": self.candidate_hash,
            "protected_manifest_hash": self.protected_manifest_hash,
            "source_diff_hash": self.source_diff_hash,
            "archive_path": self.archive_path,
            "implementation_run_id": self.implementation_run_id,
            "candidate_timestamp": self.candidate_timestamp,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FrozenCandidate:
        return cls(**{key: str(value.get(key) or "") for key in (
            "workspace", "baseline_hash", "candidate_hash", "protected_manifest_hash",
            "source_diff_hash", "archive_path", "implementation_run_id",
            "candidate_timestamp")})


class CandidateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def freeze(
        self, workspace: Path, *, implementation_run_id: str,
        baseline: WorkspaceSnapshot | None = None,
    ) -> FrozenCandidate:
        if not implementation_run_id.strip():
            raise AutonomyContractError("implementation run id is required to freeze candidate")
        snapshot = snapshot_workspace(workspace)
        diff: dict[str, tuple[str | None, str | None]] = {}
        if baseline is not None:
            for name in sorted(set(baseline.production_files) | set(snapshot.production_files)):
                old = baseline.production_files.get(name)
                new = snapshot.production_files.get(name)
                if old != new:
                    diff[name] = (old, new)
        archive = self.root / f"{snapshot.tree_hash}.tar"
        temporary = archive.with_suffix(".tmp")
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as bundle:
            for name in sorted(snapshot.files):
                source = Path(snapshot.workspace) / name
                info = bundle.gettarinfo(str(source), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with source.open("rb") as handle:
                    bundle.addfile(info, handle)
        os.replace(temporary, archive)
        archive.chmod(0o400)
        return FrozenCandidate(
            workspace=snapshot.workspace,
            baseline_hash=baseline.tree_hash if baseline is not None else snapshot.tree_hash,
            candidate_hash=snapshot.tree_hash,
            protected_manifest_hash=_json_hash(snapshot.protected_files),
            source_diff_hash=_json_hash(diff),
            archive_path=str(archive),
            implementation_run_id=implementation_run_id,
            candidate_timestamp=datetime.now(UTC).isoformat(),
        )

    @contextmanager
    def materialize(self, candidate: FrozenCandidate) -> Iterator[Path]:
        archive = Path(candidate.archive_path)
        if not archive.is_file():
            raise AutonomyContractError("candidate archive is missing")
        with tempfile.TemporaryDirectory(prefix="antigona-verify-") as directory:
            target = Path(directory)
            with tarfile.open(archive, "r") as bundle:
                for member in bundle.getmembers():
                    relative = PurePosixPath(member.name)
                    if relative.is_absolute() or ".." in relative.parts or not member.isfile():
                        raise AutonomyContractError("unsafe candidate archive member")
                    destination = target.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise AutonomyContractError("candidate archive member is unreadable")
                    with destination.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
            actual = snapshot_workspace(target)
            if actual.tree_hash != candidate.candidate_hash:
                raise AutonomyContractError("candidate hash mismatch")
            if _json_hash(actual.protected_files) != candidate.protected_manifest_hash:
                raise AutonomyContractError("protected manifest mismatch")
            yield target


@dataclass(frozen=True)
class StageContext:
    stage: str
    workspace: Path
    snapshot_hash: str = ""
    before: WorkspaceSnapshot | None = None
    mutation_required: bool = False
    candidate_store: Path | None = None
    candidate: FrozenCandidate | None = None
    implementer_service: str = ""
    verifier_service: str = ""
    test_command: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    produced_result: dict[str, Any] | None = None
    implementation_run_id: str = ""

    @classmethod
    def analysis(cls, workspace: Path, snapshot_hash: str) -> StageContext:
        return cls("analysis", workspace, snapshot_hash=snapshot_hash)

    @classmethod
    def implementation(
        cls, workspace: Path, before: WorkspaceSnapshot, *, mutation_required: bool,
        candidate_store: Path | None = None, implementation_run_id: str = "",
    ) -> StageContext:
        return cls("implementation", workspace, before=before,
                   mutation_required=mutation_required, candidate_store=candidate_store,
                   implementation_run_id=implementation_run_id)

    @classmethod
    def verification(
        cls, workspace: Path, candidate: FrozenCandidate, *, implementer_service: str,
        verifier_service: str, test_command: Sequence[str], criteria: Sequence[str],
    ) -> StageContext:
        return cls("verification", workspace, candidate=candidate,
                   implementer_service=implementer_service, verifier_service=verifier_service,
                   test_command=tuple(test_command), criteria=tuple(criteria))

    @classmethod
    def verification_without_candidate(
        cls, workspace: Path, *, implementer_service: str, verifier_service: str,
        test_command: Sequence[str], criteria: Sequence[str], baseline_hash: str,
    ) -> StageContext:
        return cls("verification", workspace, snapshot_hash=baseline_hash,
                   implementer_service=implementer_service, verifier_service=verifier_service,
                   test_command=tuple(test_command), criteria=tuple(criteria))

    @classmethod
    def result_producer(cls, workspace: Path, input_hash: str) -> StageContext:
        return cls("result", workspace, snapshot_hash=input_hash)

    @classmethod
    def result_verifier(
        cls, workspace: Path, input_hash: str, *, producer_service: str,
        verifier_service: str, produced_result: dict[str, Any],
        criteria: Sequence[str] = (),
    ) -> StageContext:
        return cls("result_verification", workspace, snapshot_hash=input_hash,
                   implementer_service=producer_service, verifier_service=verifier_service,
                   produced_result=produced_result, criteria=tuple(criteria))


@dataclass(frozen=True)
class StageEvaluation:
    evidence: dict[str, Any]
    candidate: FrozenCandidate | None = None


def _object(output: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AutonomyContractError("stage output must be one JSON object") from exc
    if not isinstance(value, dict):
        raise AutonomyContractError("stage output must be one JSON object")
    return value


def evaluate_stage(context: StageContext, output: str) -> StageEvaluation:
    payload = _object(output)
    workspace = WorkspaceBoundary(context.workspace, writable=False).validate()
    declared_workspace = str(payload.get("workspace") or str(workspace))
    if Path(declared_workspace).absolute() != workspace:
        raise AutonomyContractError("stage evidence is bound to the wrong workspace")

    if context.stage == "analysis":
        if payload.get("kind") != "analysis" or payload.get("snapshot_hash") != context.snapshot_hash:
            raise AutonomyContractError("analysis is not bound to the declared snapshot")
        evidence = payload.get("evidence")
        if not str(payload.get("summary") or "").strip() or not isinstance(evidence, list) or not evidence:
            raise AutonomyContractError("analysis requires independently checkable evidence")
        for item in evidence:
            if not isinstance(item, dict):
                raise AutonomyContractError("invalid analysis evidence")
            relative = PurePosixPath(str(item.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise AutonomyContractError("analysis evidence escapes workspace")
            path = workspace.joinpath(*relative.parts)
            line = item.get("line")
            if not path.is_file() or hash_file(path) != item.get("sha256"):
                raise AutonomyContractError("analysis evidence does not match workspace")
            if not isinstance(line, int) or line < 1:
                raise AutonomyContractError("analysis evidence requires a source line")
            with path.open(encoding="utf-8", errors="replace") as handle:
                if line > sum(1 for _ in handle):
                    raise AutonomyContractError("analysis evidence line is outside the file")
        return StageEvaluation({**payload, "workspace": str(workspace)})

    if context.stage == "implementation":
        if payload.get("kind") != "implementation" or context.before is None:
            raise AutonomyContractError("invalid implementation result")
        after = snapshot_workspace(workspace)
        if after.protected_files != context.before.protected_files:
            raise AutonomyContractError("protected tests/config changed")
        changed = {
            name for name in set(after.production_files) | set(context.before.production_files)
            if after.production_files.get(name) != context.before.production_files.get(name)
        }
        if context.mutation_required and not changed:
            raise AutonomyContractError("mutation-required implementation has no production source diff")
        store_root = context.candidate_store or workspace / ".antigona" / "candidates"
        candidate = CandidateStore(store_root).freeze(
            workspace, baseline=context.before,
            implementation_run_id=context.implementation_run_id,
        )
        evidence = {**payload, **candidate.as_dict(),
                    "changed_production_files": sorted(changed)}
        return StageEvaluation(evidence, candidate)

    if context.stage == "verification":
        frozen_candidate = context.candidate
        if frozen_candidate is None:
            raise AutonomyContractError("candidate provenance is missing")
        if not frozen_candidate.implementation_run_id or not frozen_candidate.candidate_timestamp:
            raise AutonomyContractError("candidate run provenance is missing")
        if snapshot_workspace(workspace).tree_hash != frozen_candidate.candidate_hash:
            raise AutonomyContractError("authoritative workspace no longer matches candidate")
        if not context.implementer_service or context.implementer_service == context.verifier_service:
            raise AutonomyContractError("verifier is not independent")
        if (payload.get("kind") != "verification"
                or payload.get("candidate_hash") != frozen_candidate.candidate_hash):
            raise AutonomyContractError("verification candidate hash is stale or mismatched")
        evaluations = payload.get("criteria")
        if not isinstance(evaluations, list) or len(evaluations) != len(context.criteria):
            raise AutonomyContractError("per-criterion evaluation is incomplete")
        by_name = {str(item.get("criterion")): item for item in evaluations if isinstance(item, dict)}
        if any(name not in by_name or by_name[name].get("passed") is not True
               or not str(by_name[name].get("evidence") or "").strip() for name in context.criteria):
            raise AutonomyContractError("acceptance criterion failed or lacks evidence")
        store = CandidateStore(Path(frozen_candidate.archive_path).parent)
        with store.materialize(frozen_candidate) as copy:
            tested_hash = snapshot_workspace(copy).tree_hash
            test = WorkspaceBoundary(copy, writable=False).run(context.test_command, timeout=120)
        if test.returncode != 0:
            raise AutonomyContractError(f"tests failed with exit code {test.returncode}")
        evidence = {
            **payload,
            **frozen_candidate.as_dict(),
            "workspace": frozen_candidate.workspace,
            "implementer_service": context.implementer_service,
            "verifier_service": context.verifier_service,
            "tested_candidate_hash": tested_hash,
            "test_command": list(context.test_command),
            "test_exit_code": test.returncode,
            "test_output": (test.stdout + test.stderr)[-4000:],
            "read_only_boundary": "bubblewrap",
        }
        return StageEvaluation(evidence, frozen_candidate)

    if context.stage == "result":
        if payload.get("kind") != "result" or payload.get("input_hash") != context.snapshot_hash:
            raise AutonomyContractError("producer result is not bound to the input snapshot")
        if "result" not in payload or not str(payload.get("derivation") or "").strip():
            raise AutonomyContractError("producer result or derivation is missing")
        evidence = {**payload, "workspace": str(workspace)}
        evidence["result_hash"] = _json_hash(payload["result"])
        return StageEvaluation(evidence)

    if context.stage == "result_verification":
        produced = context.produced_result
        if not produced:
            raise AutonomyContractError("result was first produced by verifier")
        if snapshot_workspace(workspace).tree_hash != context.snapshot_hash:
            raise AutonomyContractError("result input snapshot changed before verification")
        if not context.implementer_service or context.implementer_service == context.verifier_service:
            raise AutonomyContractError("result verifier is not independent")
        evaluations = payload.get("criteria")
        criteria_ok = True
        if context.criteria:
            if not isinstance(evaluations, list) or len(evaluations) != len(context.criteria):
                criteria_ok = False
            else:
                by_name = {
                    str(item.get("criterion")): item
                    for item in evaluations if isinstance(item, dict)
                }
                criteria_ok = all(
                    name in by_name and by_name[name].get("passed") is True
                    and bool(str(by_name[name].get("evidence") or "").strip())
                    for name in context.criteria
                )
        if (payload.get("kind") != "result_verification"
                or payload.get("input_hash") != context.snapshot_hash
                or payload.get("producer_result_hash") != produced.get("result_hash")
                or payload.get("passed") is not True
                or not criteria_ok
                or not str(payload.get("evidence") or "").strip()):
            raise AutonomyContractError("result verification is incomplete or mismatched")
        return StageEvaluation({**payload, "workspace": str(workspace),
                                "producer_service": context.implementer_service,
                                "verifier_service": context.verifier_service})

    raise AutonomyContractError(f"unsupported stage contract: {context.stage}")
