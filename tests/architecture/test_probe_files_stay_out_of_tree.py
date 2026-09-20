"""F-20260918T2118Z: arch-guard probe modules must never enter the frozen tree.

The guard tests used to write their probe modules into ``src/antigona/`` and
removed them only on the happy path, so a SIGKILL inside the write window left a
file behind. The file is NOT gitignored and
``src/antigona/startup/validator.py`` classifies it as an unknown deployment
file -> ``contract:C11`` CRITICAL -> the fail-closed startup gate refuses to
start the stack (same incident class as SHA256SUMS.txt on 2026-09-16).

These tests pin the out-of-tree contract:
1. the probe sandbox of the guard tests is outside the repository;
2. no ``src/antigona/_*probe*.py`` survives in the real tree;
3. the sandbox copy is faithful (clean tree -> rc 0) and still discriminating
   (probe present -> rc != 0 and the probe file is named);
4. SIGKILL of the probe node inside the write window leaves the real tree clean.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROBE_NODE = (
    "tests/architecture/test_p1_003_arch_guard_chokepoint.py"
    "::test_p1_003_new_forbidden_path_construction_fails"
)
SANDBOX_ENV = "ANTIGONA_ARCH_GUARD_SANDBOX"
HOLD_ENV = "ANTIGONA_ARCH_GUARD_PROBE_HOLD_SECONDS"
PROBE_GLOB = "_*probe*.py"
#: Bounded wait for the nested pytest node (lesson B43: never spawn without timeout).
KILL_TIMEOUT_S = 180.0
HOLD_S = "30"


def _make_sandbox(dest: Path) -> Path:
    """Copy the minimal tree ``scripts/arch_guard.py`` scans into *dest*."""
    (dest / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "arch_guard.py", dest / "scripts" / "arch_guard.py")
    shutil.copy2(
        REPO_ROOT / "scripts" / "arch_baseline.txt", dest / "scripts" / "arch_baseline.txt"
    )
    shutil.copytree(
        REPO_ROOT / "src" / "antigona",
        dest / "src" / "antigona",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    return dest


def _guard_cmd(sandbox: Path) -> list[str]:
    return [
        sys.executable,
        str(sandbox / "scripts" / "arch_guard.py"),
        "--baseline",
        str(sandbox / "scripts" / "arch_baseline.txt"),
    ]


def _probes_in_real_tree() -> list[str]:
    src = REPO_ROOT / "src" / "antigona"
    if not src.is_dir():
        return []
    return sorted(p.name for p in src.glob(PROBE_GLOB))


def _porcelain_src() -> list[str]:
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", "src/"],
        capture_output=True, text=True, check=True,
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _porcelain_path(entry: str) -> str:
    """Path field of a porcelain-v1 line (``XY PATH``; renames are ``old -> new``)."""
    field = entry[3:].strip() if len(entry) > 3 else ""
    if " -> " in field:
        field = field.split(" -> ", 1)[1].strip()
    return field


def _probe_named_porcelain_entries() -> list[str]:
    """Porcelain entries under src/ whose path names a probe module.

    Only probe-named leaks fail the contract (OBS-20260918T2248Z): an unrelated
    uncommitted src/ edit during a writer wave is legitimate and must not paint
    this node RED.
    """
    return [
        entry
        for entry in _porcelain_src()
        if fnmatch.fnmatch(Path(_porcelain_path(entry)).name, PROBE_GLOB)
    ]


def test_probe_sandbox_path_is_outside_repository(tmp_path: Path) -> None:
    sandbox = _make_sandbox(tmp_path / "guard_sandbox")
    probe = sandbox / "src" / "antigona" / "_p1_003_probe_home.py"
    assert not probe.is_relative_to(REPO_ROOT)
    assert probe.parent == sandbox / "src" / "antigona"
    # the sandbox must be a faithful copy: guard + baseline + the scanned tree
    assert (sandbox / "scripts" / "arch_guard.py").is_file()
    assert (sandbox / "scripts" / "arch_baseline.txt").is_file()
    assert (sandbox / "src" / "antigona" / "core" / "paths.py").is_file()


def test_no_probe_files_left_in_real_tree() -> None:
    """Bounded post-condition: the guard probe tests leave the repo untouched."""
    assert _probes_in_real_tree() == [], "probe module leaked into the frozen tree"
    # OBS-20260918T2248Z: fail only on a leaked probe module, not on any src/ diff.
    assert _probe_named_porcelain_entries() == [], (
        "probe module leaked into the frozen tree (git status --porcelain -- src/)"
    )


def test_sandbox_guard_is_faithful_and_discriminating(tmp_path: Path) -> None:
    """The out-of-tree negative test is not vacuous: clean -> 0, probe -> != 0."""
    sandbox = _make_sandbox(tmp_path / "guard_sandbox")
    clean = subprocess.run(_guard_cmd(sandbox), cwd=str(sandbox), capture_output=True, text=True)
    assert clean.returncode == 0, f"clean sandbox must pass:\n{clean.stdout}\n{clean.stderr}"
    probe = sandbox / "src" / "antigona" / "_arch_probe_discriminator.py"
    assert not probe.is_relative_to(REPO_ROOT)
    probe.write_text("from pathlib import Path\nBAD = Path.home()\n", encoding="utf-8")
    try:
        dirty = subprocess.run(
            _guard_cmd(sandbox), cwd=str(sandbox), capture_output=True, text=True
        )
        assert dirty.returncode != 0
        assert "_arch_probe_discriminator.py" in dirty.stdout
    finally:
        probe.unlink(missing_ok=True)


def test_sigkill_in_probe_window_leaves_real_tree_clean(tmp_path: Path) -> None:
    """Kill falsifier: SIGKILL the probe node while its probe file exists.

    The sandbox is pinned via ``ANTIGONA_ARCH_GUARD_SANDBOX`` and the node holds
    the probe file for ``HOLD_S`` seconds (``ANTIGONA_ARCH_GUARD_PROBE_HOLD_SECONDS``),
    so the kill is deterministic: the file provably exists at kill time — in the
    sandbox — while the frozen tree must stay clean.
    """
    sandbox = tmp_path / "guard_sandbox"
    assert _probes_in_real_tree() == [], "precondition: real tree clean"
    env = {**os.environ, "PYTHONPATH": "src", SANDBOX_ENV: str(sandbox), HOLD_ENV: HOLD_S}
    child_log = tmp_path / "kill_child.log"
    with child_log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", PROBE_NODE, "-q", "-p", "no:cacheprovider", "--tb=no"],
            cwd=str(REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    observed: list[Path] = []
    deadline = time.monotonic() + KILL_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            hits = sorted((sandbox / "src" / "antigona").glob(PROBE_GLOB)) \
                if (sandbox / "src" / "antigona").is_dir() else []
            if hits:
                observed = hits
                break
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        assert proc.poll() is None, (
            f"probe node exited before the kill window (rc={proc.returncode}):\n"
            f"{child_log.read_text(encoding='utf-8')}"
        )
        assert observed, f"probe file never appeared in the sandbox:\n{child_log.read_text()}"
        assert all(not p.is_relative_to(REPO_ROOT) for p in observed)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=60)
        time.sleep(0.3)
        assert _probes_in_real_tree() == [], "SIGKILL left a probe module in the frozen tree"
        # OBS-20260918T2248Z: only a probe-named leak fails this node.
        assert _probe_named_porcelain_entries() == [], (
            "SIGKILL left a probe module in the frozen tree (git status --porcelain -- src/)"
        )
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=60)
        shutil.rmtree(sandbox, ignore_errors=True)
    assert not sandbox.exists(), "sandbox must be gone"
