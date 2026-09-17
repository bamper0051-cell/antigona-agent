"""Drift and regression guard tests for root-agnostic systemd units and installer.

Ensures that:
1. Every deploy/systemd/*.service unit uses @ANTIGONA_ROOT@ for install-tree paths
   (WorkingDirectory, ExecStart, BindReadOnlyPaths) with no hardcoded install roots.
2. deploy/systemd/install_units.sh is present, executable, and passes syntax validation (bash -n).
3. The installer enforces valid ANTIGONA_ROOT containing src/antigona.
4. The installer --dry-run renders all units with the root substituted and zero leftover placeholders.
5. The installer writes rendered units to the target destination idempotently.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from antigona.core import paths

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
INSTALLER_SCRIPT = DEPLOY_SYSTEMD_DIR / "install_units.sh"

ALLOWED_NON_ROOT_PREFIXES = (
    "@ANTIGONA_ROOT@",
    # B34: the out-of-tree env file and the uv interpreter are no longer literal
    # home paths in the shipped units; they are tokens the installer renders from
    # the installing user's home (install_units.sh render_file()).
    "@ANTIGONA_ENV_FILE@",
    "@ANTIGONA_UV_PYTHON@",
    "/var/lib/antigona",
    "/var/log/antigona",
    "/run/antigona",
    "/etc/antigona/antigona.env",
    # Derived from the canonical home helper instead of a hardcoded owner path.
    str(paths.home_dir() / ".local" / "share" / "uv" / "python"),
    "/usr/bin/python3",
    "/var/run/docker.sock",
)


def test_repo_service_units_have_no_hardcoded_install_roots() -> None:
    """Assert that all unit path directives use @ANTIGONA_ROOT@ or allowed non-root paths."""
    service_files = list(DEPLOY_SYSTEMD_DIR.glob("*.service"))
    assert len(service_files) >= 7, f"Expected at least 7 service files, found {len(service_files)}"

    for svc in service_files:
        lines = svc.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, start=1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            if line.startswith("WorkingDirectory="):
                val = line.split("=", 1)[1].strip()
                assert val.startswith("@ANTIGONA_ROOT@") or any(
                    val.startswith(p) for p in ALLOWED_NON_ROOT_PREFIXES
                ), f"{svc.name}:{idx} WorkingDirectory has hardcoded path: {val}"
                assert "/opt/antigona-home/antigona" not in val, f"{svc.name}:{idx} WorkingDirectory leaks /opt/antigona-home/antigona: {val}"
                assert "/opt/antigona-home/.antigona" not in val, f"{svc.name}:{idx} WorkingDirectory leaks /opt/antigona-home/.antigona: {val}"

            elif line.startswith("ExecStart="):
                val = line.split("=", 1)[1].strip()
                assert "@ANTIGONA_ROOT@" in val or any(
                    val.startswith(p) for p in ALLOWED_NON_ROOT_PREFIXES
                ), f"{svc.name}:{idx} ExecStart missing @ANTIGONA_ROOT@: {val}"
                assert "/opt/antigona-home/antigona" not in val, f"{svc.name}:{idx} ExecStart leaks /opt/antigona-home/antigona: {val}"
                assert "/opt/antigona-home/.antigona" not in val, f"{svc.name}:{idx} ExecStart leaks /opt/antigona-home/.antigona: {val}"

            elif line.startswith("BindReadOnlyPaths="):
                val = line.split("=", 1)[1].strip()
                for token in val.split():
                    # Handle source:dest bindings if any
                    src_path = token.split(":", 1)[0]
                    assert src_path.startswith("@ANTIGONA_ROOT@") or any(
                        src_path.startswith(p) for p in ALLOWED_NON_ROOT_PREFIXES
                    ), f"{svc.name}:{idx} BindReadOnlyPaths path not allowed: {src_path}"
                    assert "/opt/antigona-home/antigona" not in src_path, f"{svc.name}:{idx} BindReadOnlyPaths leaks /opt/antigona-home/antigona: {src_path}"
                    assert "/opt/antigona-home/.antigona" not in src_path or src_path == "/etc/antigona/antigona.env", (
                        f"{svc.name}:{idx} BindReadOnlyPaths leaks /opt/antigona-home/.antigona: {src_path}"
                    )


def test_installer_script_exists_and_is_executable_and_passes_bash_n() -> None:
    """Assert installer script exists, has execute permissions, and passes syntax check."""
    assert INSTALLER_SCRIPT.is_file(), f"Installer script not found at {INSTALLER_SCRIPT}"

    file_stat = os.stat(INSTALLER_SCRIPT)
    is_executable = bool(file_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    assert is_executable, f"Installer script is not executable: {INSTALLER_SCRIPT}"

    res = subprocess.run(["bash", "-n", str(INSTALLER_SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, f"bash -n failed for {INSTALLER_SCRIPT}:\n{res.stderr}"


def test_installer_validates_antigona_root_and_flags(tmp_path: Path) -> None:
    """Assert installer rejects non-existent root or root missing src/antigona."""
    # Missing argument
    res_no_args = subprocess.run([str(INSTALLER_SCRIPT)], capture_output=True, text=True)
    assert res_no_args.returncode != 0
    assert "required" in res_no_args.stderr.lower() or "usage" in res_no_args.stderr.lower()

    # Non-existent path
    non_existent = tmp_path / "does_not_exist"
    res_non_existent = subprocess.run([str(INSTALLER_SCRIPT), str(non_existent)], capture_output=True, text=True)
    assert res_non_existent.returncode != 0
    assert "not an existing directory" in res_non_existent.stderr

    # Directory exists but lacks src/antigona
    empty_dir = tmp_path / "empty_root"
    empty_dir.mkdir()
    res_missing_src = subprocess.run([str(INSTALLER_SCRIPT), str(empty_dir)], capture_output=True, text=True)
    assert res_missing_src.returncode != 0
    assert "does not contain src/antigona" in res_missing_src.stderr


def test_installer_dry_run_against_fake_root(tmp_path: Path) -> None:
    """Assert --dry-run renders all units with fake root and no leftover placeholders."""
    fake_root = tmp_path / "custom-antigona-root"
    (fake_root / "src" / "antigona").mkdir(parents=True)

    res = subprocess.run(
        [str(INSTALLER_SCRIPT), str(fake_root), "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Dry-run failed:\n{res.stderr}"
    output = res.stdout

    # No unresolved placeholder of ANY kind must remain in output (B34).
    assert "@ANTIGONA_ROOT@" not in output, f"Found leftover @ANTIGONA_ROOT@ in dry-run output:\n{output}"
    leftover = re.findall(r"@ANTIGONA_[A-Z_]+@", output)
    assert not leftover, f"Found unrendered placeholder token(s) {sorted(set(leftover))} in dry-run output:\n{output}"

    # Fake root must be rendered in each service
    service_files = list(DEPLOY_SYSTEMD_DIR.glob("*.service"))
    for svc in service_files:
        assert svc.name in output, f"Service {svc.name} missing from dry-run output"

    assert str(fake_root) in output, f"Fake root path {fake_root} missing from dry-run output"


def test_installer_dest_write_and_idempotency(tmp_path: Path) -> None:
    """Assert installer writes rendered units to --dest directory and is idempotent."""
    fake_root = tmp_path / "test-antigona-root"
    (fake_root / "src" / "antigona").mkdir(parents=True)
    dest_dir = tmp_path / "systemd_dest"

    # Run installation to custom dest
    res1 = subprocess.run(
        [str(INSTALLER_SCRIPT), str(fake_root), "--dest", str(dest_dir)],
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, f"Install failed:\n{res1.stderr}"

    service_files = list(DEPLOY_SYSTEMD_DIR.glob("*.service"))
    for svc in service_files:
        installed_file = dest_dir / svc.name
        assert installed_file.is_file(), f"Expected installed unit at {installed_file}"
        content = installed_file.read_text(encoding="utf-8")
        assert "@ANTIGONA_ROOT@" not in content
        leftover = re.findall(r"@ANTIGONA_[A-Z_]+@", content)
        assert not leftover, f"{svc.name}: unrendered placeholder token(s) {sorted(set(leftover))}"
        assert str(fake_root) in content

    # Second run for idempotency
    res2 = subprocess.run(
        [str(INSTALLER_SCRIPT), str(fake_root), "--dest", str(dest_dir)],
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"Second install (idempotency check) failed:\n{res2.stderr}"
