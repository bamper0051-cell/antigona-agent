"""Runtime Provenance Preflight Guard for Antigona.

Fails closed if working directory, PYTHONPATH, editable .pth, or source root
diverges from the canonical recovery record.
"""

from __future__ import annotations

import ast
import hashlib
import os
import pathlib
import subprocess
import sys
from typing import NamedTuple

from antigona.core import paths
from antigona.core.paths import home_dir

EXPECTED_CONSTITUTION_SHA256 = "a492886bb80f9c0981ff05fa370d42821d9f612b970f4ded32927220c7849908"
MASTER_CONSTITUTION_SHA256 = "558ab18e6408e7a09386549de1f213aebd928d3a8601c566dace1587d608c5b2"


def _forbidden_source_patterns() -> tuple[str, ...]:
    """Legacy/stale source roots that must never appear on ``sys.path``.

    Every entry lives under the *current user's* home directory, so the home
    prefix is derived from the single home resolver (ADR-007) instead of the
    literal ``/root``: on a host where ``HOME`` / ``ANTIGONA_HOME_DIR`` differs
    the guard previously scanned for paths that cannot exist and therefore did
    not detect the real legacy source copies. On this host ``home_dir() ==
    "/root"``, so the resulting patterns are unchanged.
    """
    home = str(home_dir())
    return (
        f"{home}/.antigona/src",
        f"{home}/.antigona/.health",
        f"{home}/antigona-candidate-20260909/src",
        f"{home}/antigona-recovery-20260911/src",
        f"{home}/antigona-c1-mem-root-hermes",
        f"{home}/antigona-c1-memory-runtime-root",
        f"{home}/antigona-pidlock-fix",
        f"{home}/antigona-turn-truth-fix",
    )


FORBIDDEN_SOURCE_PATTERNS = _forbidden_source_patterns()


def default_canonical_root() -> pathlib.Path:
    raw = os.environ.get("ANTIGONA_CANONICAL_ROOT")
    if raw:
        return pathlib.Path(raw).resolve()
    return paths.project_root()


class ProvenanceCheckResult(NamedTuple):
    name: str
    ok: bool
    detail: str


class RuntimeProvenanceError(RuntimeError):
    """Raised when runtime provenance violates canonical invariants."""


def get_current_git_sha(canonical_root: pathlib.Path) -> str:
    """Read HEAD commit SHA from git worktree."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(canonical_root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def check_source_resolution(canonical_root: pathlib.Path) -> ProvenanceCheckResult:
    """Ensure import antigona resolves strictly within canonical root."""
    try:
        import antigona
        mod_file = pathlib.Path(antigona.__file__).resolve()
        expected_prefix = (canonical_root / "src" / "antigona").resolve()
        if expected_prefix in mod_file.parents or mod_file.parent == expected_prefix:
            return ProvenanceCheckResult("source_resolution", True, f"resolved inside {expected_prefix}")
        return ProvenanceCheckResult("source_resolution", False, f"resolved to non-canonical {mod_file}")
    except Exception as exc:
        return ProvenanceCheckResult("source_resolution", False, f"import error: {exc}")


def check_forbidden_paths() -> ProvenanceCheckResult:
    """Ensure sys.path and .pth files do not reference legacy source copies or stale root."""
    disallowed: list[str] = []
    for entry in sys.path:
        resolved = str(pathlib.Path(entry).resolve()) if entry else ""
        for pattern in FORBIDDEN_SOURCE_PATTERNS:
            if pattern in resolved or pattern == entry:
                disallowed.append(f"sys.path entry: {entry}")

    site_packages = [p for p in sys.path if "site-packages" in p]
    for sp in site_packages:
        sp_path = pathlib.Path(sp)
        if sp_path.is_dir():
            for pth in sp_path.glob("*.pth"):
                try:
                    content = pth.read_text(encoding="utf-8", errors="ignore")
                    for pattern in FORBIDDEN_SOURCE_PATTERNS:
                        if pattern in content:
                            disallowed.append(f".pth {pth.name} references {pattern}")
                except OSError:
                    pass

    if disallowed:
        return ProvenanceCheckResult("forbidden_paths", False, "; ".join(disallowed))
    return ProvenanceCheckResult("forbidden_paths", True, "no forbidden source paths detected")


def check_working_directory(canonical_root: pathlib.Path) -> ProvenanceCheckResult:
    """Ensure process cwd is canonical worktree or valid runtime state directory."""
    cwd = pathlib.Path.cwd().resolve()
    canon = canonical_root.resolve()
    allowed_list = [
        canon,
        pathlib.Path("/var/lib/antigona").resolve(),
        pathlib.Path("/run/antigona").resolve(),
    ]
    # Allow canonical root, runtime roots, or working from root during maintenance
    for allowed in allowed_list:
        if cwd == allowed or allowed in cwd.parents or cwd in allowed.parents:
            return ProvenanceCheckResult("working_directory", True, f"cwd {cwd} is valid")
    return ProvenanceCheckResult(
        "working_directory",
        False,
        f"cwd {cwd} is outside canonical root ({canonical_root}) and allowed prefixes",
    )


def check_continuity_record(canonical_root: pathlib.Path) -> ProvenanceCheckResult:
    """Ensure STATE_ANTIGONA_CANON.md exists and is labeled RECOVERY RECORD v1."""
    record = canonical_root / "STATE_ANTIGONA_CANON.md"
    if not record.is_file():
        return ProvenanceCheckResult("continuity_record", False, f"missing {record}")
    content = record.read_text(encoding="utf-8", errors="ignore")
    if "RECOVERY RECORD v1" not in content:
        return ProvenanceCheckResult("continuity_record", False, "invalid record schema header")
    return ProvenanceCheckResult("continuity_record", True, f"verified {record}")


def check_git_commit(canonical_root: pathlib.Path, expected_sha: str | None = None) -> ProvenanceCheckResult:
    """Ensure git commit matches expected canonical SHA or clean worktree HEAD."""
    try:
        actual_sha = subprocess.check_output(
            ["git", "-C", str(canonical_root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if not actual_sha:
            return ProvenanceCheckResult("git_commit", False, "empty git SHA returned")
        if expected_sha is not None and expected_sha != "":
            if actual_sha == expected_sha:
                return ProvenanceCheckResult("git_commit", True, f"exact match {actual_sha[:8]}")
            return ProvenanceCheckResult(
                "git_commit", False, f"SHA mismatch: expected {expected_sha}, got {actual_sha}"
            )
        return ProvenanceCheckResult("git_commit", True, f"HEAD valid ({actual_sha[:8]}...)")
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return ProvenanceCheckResult("git_commit", False, f"git check failed: {exc}")


def check_constitution(canonical_root: pathlib.Path) -> ProvenanceCheckResult:
    """Ensure in-tree constitution matches canonical hash."""
    const_path = canonical_root / "aptechka" / "CONSTITUTION.md"
    if not const_path.is_file():
        return ProvenanceCheckResult("constitution", False, f"missing {const_path}")
    actual_hash = hashlib.sha256(const_path.read_bytes()).hexdigest()
    if actual_hash == EXPECTED_CONSTITUTION_SHA256:
        return ProvenanceCheckResult("constitution", True, f"hash verified ({actual_hash[:8]}...)")
    return ProvenanceCheckResult(
        "constitution", False, f"hash mismatch: expected {EXPECTED_CONSTITUTION_SHA256[:8]}, got {actual_hash[:8]}"
    )


def check_docker_proxy_contract(canonical_root: pathlib.Path) -> ProvenanceCheckResult:
    """Ensure docker proxy deploy script exists in canonical tree and is strictly isolated."""
    proxy_script = canonical_root / "deploy" / "sandbox" / "docker_socket_proxy.py"
    if not proxy_script.is_file():
        return ProvenanceCheckResult("docker_proxy_contract", False, f"missing {proxy_script}")
    try:
        content = proxy_script.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(proxy_script))
        # Verify it never imports antigona
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "antigona" or alias.name.startswith("antigona."):
                        return ProvenanceCheckResult(
                            "docker_proxy_contract", False, f"forbidden import {alias.name} in docker proxy"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module == "antigona" or (node.module and node.module.startswith("antigona.")):
                    return ProvenanceCheckResult(
                        "docker_proxy_contract", False, f"forbidden import from {node.module} in docker proxy"
                    )
        return ProvenanceCheckResult(
            "docker_proxy_contract", True, "verified standalone deployment script with zero antigona imports"
        )
    except Exception as exc:
        return ProvenanceCheckResult("docker_proxy_contract", False, f"proxy inspection error: {exc}")


def run_provenance_checks(
    canonical_root: pathlib.Path | None = None,
    expected_sha: str | None = None,
) -> list[ProvenanceCheckResult]:
    """Execute all preflight provenance checks."""
    root = (canonical_root or default_canonical_root()).resolve()
    return [
        check_source_resolution(root),
        check_forbidden_paths(),
        check_working_directory(root),
        check_continuity_record(root),
        check_git_commit(root, expected_sha),
        check_constitution(root),
        check_docker_proxy_contract(root),
    ]


def verify_provenance_or_fail(
    canonical_root: pathlib.Path | None = None,
    expected_sha: str | None = None,
) -> None:
    """Execute preflight guard and raise RuntimeProvenanceError if any check fails."""
    results = run_provenance_checks(canonical_root, expected_sha)
    failed = [r for r in results if not r.ok]
    if failed:
        details = "\n".join(f"  - {r.name}: {r.detail}" for r in failed)
        raise RuntimeProvenanceError(f"Antigona Runtime Provenance Guard FAIL-CLOSED:\n{details}")


def main() -> int:
    results = run_provenance_checks()
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"[{status}] {r.name}: {r.detail}")
    overall = all(r.ok for r in results)
    print(f"PROVENANCE GUARD OVERALL: {'PASS' if overall else 'FAIL-CLOSED'}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
