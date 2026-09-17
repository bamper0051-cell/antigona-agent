"""P1-003: architecture-forbidden path construction hits one guard chokepoint.

The shipped program is ``scripts/arch_guard.py`` (ADR-007). Current tree +
existing baseline must exit 0; a new forbidden construction in a
non-canonical module must exit non-zero and name the probe file.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROBES: list[tuple[str, str]] = [
    ("_p1_003_probe_home.py", "from pathlib import Path\nBAD = Path.home()\n"),
    ("_p1_003_probe_expanduser.py", "from pathlib import Path\nBAD = Path('~').expanduser()\n"),
    ("_p1_003_probe_abs_root.py", "from pathlib import Path\nBAD = Path('/root/antigona')\n"),
    ("_p1_003_probe_project_root.py", "PROJECT_ROOT = Path('/somewhere')\n"),
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


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
def test_p1_003_new_forbidden_path_construction_fails(filename: str, source: str) -> None:
    root = _repo_root()
    probe = root / "src" / "antigona" / filename
    probe.write_text(source, encoding="utf-8")
    try:
        r = _run_guard(root)
        assert r.returncode != 0
        assert filename in r.stdout
    finally:
        probe.unlink(missing_ok=True)
