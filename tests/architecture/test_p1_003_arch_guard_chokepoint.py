"""P1-003: architecture-forbidden path construction hits one guard chokepoint.

The shipped program is ``scripts/arch_guard.py`` (ADR-007). Current tree +
existing baseline must exit 0; a new forbidden construction in a
non-canonical module must exit non-zero and name the probe file.

The negative probes are written into a throwaway sandbox OUTSIDE the repository
(F-20260918T2118Z): the frozen tree must never hold a probe module even
transiently, because the file is not gitignored and
``src/antigona/startup/validator.py`` fails closed on unknown deployment files.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROBES: list[tuple[str, str]] = [
    ("_p1_003_probe_home.py", "from pathlib import Path\nBAD = Path.home()\n"),
    ("_p1_003_probe_expanduser.py", "from pathlib import Path\nBAD = Path('~').expanduser()\n"),
    ("_p1_003_probe_abs_root.py", "from pathlib import Path\nBAD = Path('/root/antigona')\n"),
    ("_p1_003_probe_project_root.py", "PROJECT_ROOT = Path('/somewhere')\n"),
]

#: Test-only overrides used by tests/architecture/test_probe_files_stay_out_of_tree.py:
#: ``SANDBOX_ENV`` pins the sandbox location and ``HOLD_ENV`` holds the probe file
#: for N seconds so the SIGKILL falsifier can kill the node deterministically
#: inside the write window. Both are inert unless explicitly set.
SANDBOX_ENV = "ANTIGONA_ARCH_GUARD_SANDBOX"
HOLD_ENV = "ANTIGONA_ARCH_GUARD_PROBE_HOLD_SECONDS"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def make_guard_sandbox(dest: Path) -> Path:
    """Copy the minimal tree ``scripts/arch_guard.py`` needs, OUTSIDE the repo.

    The guard scans ``src/antigona/**/*.py`` and ``tests/**/*.py`` relative to its
    cwd, so a sandbox copy reproduces its verdict exactly while keeping every
    probe module out of the frozen tree.
    """
    root = _repo_root()
    (dest / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "scripts" / "arch_guard.py", dest / "scripts" / "arch_guard.py")
    shutil.copy2(root / "scripts" / "arch_baseline.txt", dest / "scripts" / "arch_baseline.txt")
    shutil.copytree(
        root / "src" / "antigona",
        dest / "src" / "antigona",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    return dest


@pytest.fixture(scope="module")
def guard_sandbox(tmp_path_factory: pytest.TempPathFactory) -> Path:
    override = os.environ.get(SANDBOX_ENV)
    dest = Path(override) if override else tmp_path_factory.mktemp("arch_guard_sandbox")
    return make_guard_sandbox(dest)


def _run_guard(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "arch_guard.py"),
            "--baseline",
            str(root / "scripts" / "arch_baseline.txt"),
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
    )


def test_p1_003_single_chokepoint_is_arch_guard_script() -> None:
    root = _repo_root()
    guard = root / "scripts" / "arch_guard.py"
    assert guard.is_file()
    text = guard.read_text(encoding="utf-8")
    assert "P1_home" in text
    assert "P2_expanduser" in text
    assert "P3_abs_root_path" in text
    assert "P4_project_root_def" in text
    peers = sorted(p.name for p in (root / "scripts").glob("*arch*guard*"))
    assert peers == ["arch_guard.py"]


def test_p1_003_current_tree_passes_baseline() -> None:
    root = _repo_root()
    r = _run_guard(root)
    assert r.returncode == 0, f"arch_guard failed:\n{r.stdout}\n{r.stderr}"


@pytest.mark.parametrize("filename,source", PROBES)
def test_p1_003_new_forbidden_path_construction_fails(
    filename: str, source: str, guard_sandbox: Path
) -> None:
    root = _repo_root()
    probe = guard_sandbox / "src" / "antigona" / filename
    assert not probe.is_relative_to(root), "probe must never be written inside the repository"
    probe.write_text(source, encoding="utf-8")
    try:
        hold = float(os.environ.get(HOLD_ENV) or "0")
        if hold > 0:
            time.sleep(hold)
        r = _run_guard(guard_sandbox)
        assert r.returncode != 0
        assert filename in r.stdout
    finally:
        probe.unlink(missing_ok=True)
