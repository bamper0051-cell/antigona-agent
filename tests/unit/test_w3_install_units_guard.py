"""B28 red/green falsifier: the systemd installer must not regress a live unit.

Background
----------
The W3 change wired ``ExecStartPre=@ANTIGONA_ROOT@/scripts/startup_gate.sh`` into
the seven ``deploy/systemd/*.service`` templates.  Those templates are 18-line
LEGACY units; the seven units actually RUNNING in production are 58-61-line
HARDENED units in ``/etc/systemd/system`` (``User=antigona-svc``,
``ProtectSystem=strict``, ``NoNewPrivileges=yes``, ...) with no ``ExecStartPre``.
Executing the documented install path therefore used to overwrite the hardened
units with the legacy templates, silently dropping the service sandbox.

This file asserts the two halves of the B28 fix, black-box, against the REAL
installer inside a ``tmp_path`` tree (Security Law 6: a fake tree in an explicit
test zone, never a mocked production path; ``/etc`` and ``systemctl`` are never
touched):

* plain install over a DIFFERENT existing unit is a hard error and leaves the
  existing file byte-identical;
* ``--force --backup-dir`` backs the old bytes up before installing;
* ``--check`` reports drift (rc=1) and writes nothing;
* re-installing identical content is idempotent (rc=0, zero bytes changed);
* ``--dropins --dry-run`` renders the gate drop-in for all seven units;
* the shipped drop-in carries exactly one gate ``ExecStartPre`` and lives outside
  the ``*.service`` glob.

On the parent commit 1e3c287 the installer has no ``--check``/``--force``/
``--backup-dir``/``--dropins`` support and writes unconditionally, so these tests
FAIL there and PASS after the fix.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
INSTALLER = DEPLOY_SYSTEMD_DIR / "install_units.sh"
DROPIN = DEPLOY_SYSTEMD_DIR / "dropins" / "10-antigona-startup-gate.conf"

UNIT_COUNT = 7
DROPIN_REL = "10-antigona-startup-gate.conf"

# A unit shaped like the real hardened live unit: distinct content, sandbox and
# privilege drop present, and NO ExecStartPre gate line.
HARDENED_UNIT = """[Unit]
Description=Antigona Gateway (hardened live unit)
After=network.target

[Service]
Type=simple
User=antigona-svc
Group=antigona-svc
WorkingDirectory=/opt/antigona-home/antigona
Environment=PYTHONPATH=/opt/antigona-home/antigona/src
Environment=ANTIGONA_IMMUTABLE_DEPLOYMENT=1
ProtectSystem=strict
ProtectHome=tmpfs
NoNewPrivileges=yes
ReadWritePaths=/var/lib/antigona /var/log/antigona /run/antigona
BindReadOnlyPaths=/etc/antigona/antigona.env
ExecStart=/opt/antigona-home/antigona/.venv/bin/python -c 'import antigona.gateway'
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unit_names() -> list[str]:
    return sorted(p.name for p in DEPLOY_SYSTEMD_DIR.glob("*.service"))


def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "candidate-root"
    (root / "src" / "antigona").mkdir(parents=True)
    return root


def _install_cmd(root: Path, *args: str) -> list[str]:
    return [str(INSTALLER), str(root), *args]


# ── (a) unchanged controller: differing existing target is a hard error ────────


def test_plain_install_refuses_to_overwrite_differing_unit(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "antigona-gateway.service"
    target.write_text(HARDENED_UNIT, encoding="utf-8")
    before = _sha256(target)

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest)),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert res.returncode != 0, f"installer must refuse the overwrite:\n{res.stdout}\n{res.stderr}"
    assert "antigona-gateway.service" in res.stderr, f"target not named:\n{res.stderr}"
    assert "regressed" in res.stderr, f"no regression warning:\n{res.stderr}"
    assert _sha256(target) == before, "the pre-existing hardened unit was modified"
    assert target.read_text(encoding="utf-8") == HARDENED_UNIT
    assert sorted(p.name for p in dest.iterdir()) == ["antigona-gateway.service"], (
        "the preflight refusal must leave the destination tree untouched"
    )


# ── (b) --force --backup-dir: backup-first, then install ──────────────────────


def test_force_with_backup_dir_preserves_old_bytes_then_installs(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "antigona-gateway.service"
    target.write_text(HARDENED_UNIT, encoding="utf-8")
    before = _sha256(target)
    backup_dir = tmp_path / "backups"

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--force", "--backup-dir", str(backup_dir)),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert res.returncode == 0, f"forced install failed:\n{res.stdout}\n{res.stderr}"
    assert str(backup_dir) in res.stdout, f"backup path not printed:\n{res.stdout}"

    backups = [p for p in backup_dir.rglob("*") if p.is_file()]
    assert backups, f"no backup file created under {backup_dir}"
    matching = [p for p in backups if _sha256(p) == before]
    assert matching, "the original hardened bytes are not preserved in the backup"
    assert matching[0].read_text(encoding="utf-8") == HARDENED_UNIT

    new_content = target.read_text(encoding="utf-8")
    assert new_content != HARDENED_UNIT, "the new unit content was not installed"
    assert "@ANTIGONA_ROOT@" not in new_content
    leftover = re.findall(r"@ANTIGONA_[A-Z_]+@", new_content)
    assert not leftover, f"unrendered placeholder token(s) {sorted(set(leftover))}"
    assert str(root) in new_content


# ── (c) --check: drift report, writes nothing ────────────────────────────────


def test_check_reports_drift_and_writes_nothing(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "dest"

    install = subprocess.run(
        _install_cmd(root, "--dest", str(dest)),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert install.returncode == 0, f"baseline install failed:\n{install.stderr}"

    drifted = dest / "antigona-worker.service"
    drifted.write_text(HARDENED_UNIT, encoding="utf-8")
    before = _sha256(drifted)
    files_before = _sha256_map(dest)

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--check"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert res.returncode == 1, f"--check must fail on drift:\n{res.stdout}\n{res.stderr}"
    assert "antigona-worker.service: DRIFTED" in res.stdout, f"drift not reported:\n{res.stdout}"
    assert _sha256(drifted) == before, "--check modified the drifted file"
    assert _sha256_map(dest) == files_before, "--check wrote files"


def test_check_passes_on_up_to_date_dest(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "dest"

    install = subprocess.run(
        _install_cmd(root, "--dest", str(dest)),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert install.returncode == 0, f"baseline install failed:\n{install.stderr}"

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--check"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert res.returncode == 0, f"--check must pass on an up-to-date dest:\n{res.stdout}\n{res.stderr}"
    assert "DRIFTED" not in res.stdout, f"unexpected drift report:\n{res.stdout}"
    assert res.stdout.count("UP-TO-DATE") == UNIT_COUNT, f"unexpected report:\n{res.stdout}"


def _sha256_map(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): _sha256(p)
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


# ── (d) idempotency: identical content needs no --force, changes no bytes ────


def test_second_identical_install_is_idempotent(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "dest"

    first = subprocess.run(
        _install_cmd(root, "--dest", str(dest)),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert first.returncode == 0, f"first install failed:\n{first.stderr}"
    before = _sha256_map(dest)
    assert len(before) == UNIT_COUNT, f"expected {UNIT_COUNT} installed units, got {len(before)}"

    second = subprocess.run(
        _install_cmd(root, "--dest", str(dest)),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert second.returncode == 0, (
        f"identical re-install must not require --force:\n{second.stdout}\n{second.stderr}"
    )
    assert _sha256_map(dest) == before, "the idempotent re-install changed bytes"


# ── (e) --dropins --dry-run renders the gate drop-in for all seven units ─────


def test_dropins_dry_run_renders_gate_for_every_unit(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "dest-never-created"

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--dropins", "--dry-run"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert res.returncode == 0, f"drop-in dry-run failed:\n{res.stdout}\n{res.stderr}"
    out = res.stdout
    assert "@ANTIGONA_ROOT@" not in out, f"unresolved placeholder in dry-run output:\n{out}"
    leftover = re.findall(r"@ANTIGONA_[A-Z_]+@", out)
    assert not leftover, f"unrendered placeholder token(s) {sorted(set(leftover))}:\n{out}"

    gate_line = f"ExecStartPre={root}/scripts/startup_gate.sh"
    for unit in _unit_names():
        dropin_path = f"{dest}/{unit}.d/{DROPIN_REL}"
        header = f"=== [DRY-RUN] {dropin_path} ==="
        assert header in out, f"missing rendered drop-in path: {dropin_path}"
        body = out.split(header, 1)[1].split("=== [DRY-RUN] ", 1)[0]
        assert body.count(gate_line) == 1, (
            f"drop-in for {unit} must render exactly one gate directive:\n{body}"
        )

    assert out.count("=== [DRY-RUN] ") == UNIT_COUNT * 2, (
        f"expected {UNIT_COUNT * 2} rendered sections (units + drop-ins):\n{out}"
    )
    assert not dest.exists(), "--dropins --dry-run created the destination directory"


def test_dropins_install_writes_only_under_dest(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "dest"

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--dropins"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert res.returncode == 0, f"drop-in install failed:\n{res.stdout}\n{res.stderr}"

    for unit in _unit_names():
        dropin = dest / f"{unit}.d" / DROPIN_REL
        assert dropin.is_file(), f"drop-in not installed: {dropin}"
        text = dropin.read_text(encoding="utf-8")
        assert f"ExecStartPre={root}/scripts/startup_gate.sh" in text
        assert "@ANTIGONA_ROOT@" not in text
        leftover = re.findall(r"@ANTIGONA_[A-Z_]+@", text)
        assert not leftover, f"unrendered placeholder token(s) {sorted(set(leftover))} in {dropin}"

    # Re-installing the identical drop-ins must be idempotent (no --force).
    second = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--dropins"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert second.returncode == 0, f"drop-in re-install failed:\n{second.stdout}\n{second.stderr}"


# ── (f) the shipped drop-in is the real, gate-only artifact ──────────────────


def test_shipped_dropin_is_gate_only_and_outside_service_glob() -> None:
    assert DROPIN.is_file(), f"shipped drop-in missing: {DROPIN}"

    lines = [
        line.strip()
        for line in DROPIN.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    exec_pre = [line for line in lines if line.startswith("ExecStartPre=")]
    assert exec_pre == ["ExecStartPre=@ANTIGONA_ROOT@/scripts/startup_gate.sh"], (
        f"drop-in must carry exactly one gate ExecStartPre, got {exec_pre}"
    )
    assert lines == ["[Service]", *exec_pre], f"drop-in must contain only the gate directive: {lines}"

    service_glob = sorted(DEPLOY_SYSTEMD_DIR.glob("*.service"))
    assert len(service_glob) == UNIT_COUNT, (
        f"expected exactly {UNIT_COUNT} *.service files, found {len(service_glob)}"
    )
    assert DROPIN.resolve() not in {p.resolve() for p in service_glob}

    text = DROPIN.read_text(encoding="utf-8")
    assert "OWNER-GATED" in text, "the drop-in must state that activation is owner-gated"


# ── (g) fail-closed is not an alias for fail-open on missing targets ─────────


def test_check_on_empty_dest_reports_missing_without_drift(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "empty-dest"
    dest.mkdir()

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--check"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert res.returncode == 0, f"absence is not drift:\n{res.stdout}\n{res.stderr}"
    assert res.stdout.count("MISSING") == UNIT_COUNT, f"unexpected report:\n{res.stdout}"


# ── (h) B30: --dropins layers onto a HARDENED unit, never regresses it ────────
#
# The real deployment dest already holds the seven 58–61-line HARDENED units
# (User=antigona-svc, ProtectSystem=strict, ProtectHome=tmpfs).  The whole point
# of the drop-in mechanism is to MERGE the ExecStartPre gate into such a unit, so
# `--dropins` must succeed there without --force and must not rewrite a single
# byte of the base units.  Before B30 the installer unconditionally added the
# legacy base templates to the write set, refused (rc=1, 0 drop-ins) and the only
# working path replaced the hardened units with 18-line legacy templates.

# A unit shaped like the real hardened live unit, at the live size (60 lines):
# sandbox and privilege drop present, and no ExecStartPre gate line.
_HARDENED_B30_LINES = (
    [
        "[Unit]",
        "Description=Antigona hardened live unit (60 lines)",
        "Documentation=man:systemd.service(5)",
        "After=network.target network-online.target",
        "Wants=network-online.target",
        "ConditionPathIsReadWrite=/var/lib/antigona",
        "",
        "[Service]",
        "Type=simple",
        "User=antigona-svc",
        "Group=antigona-svc",
        "UMask=0027",
    ]
    + [f"Environment=ANTIGONA_HARDENED_FLAG_{i}=1" for i in range(6)]
    + [
        "Environment=PYTHONPATH=/opt/antigona-home/antigona/src",
        "Environment=ANTIGONA_IMMUTABLE_DEPLOYMENT=1",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "NoNewPrivileges=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "ReadWritePaths=/var/lib/antigona /var/log/antigona /run/antigona",
        "BindReadOnlyPaths=/etc/antigona/antigona.env",
    ]
    + [f"Environment=ANTIGONA_SANDBOX_EXTRA_{i}=1" for i in range(18)]
    + [
        "ExecStart=/opt/antigona-home/antigona/.venv/bin/python -c 'import antigona.gateway'",
        "KillMode=mixed",
        "TimeoutStopSec=30",
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
)
HARDENED_UNIT_B30 = "\n".join(_HARDENED_B30_LINES) + "\n"


def test_dropins_over_hardened_dest_succeeds_and_leaves_base_units_untouched(
    tmp_path: Path,
) -> None:
    """``--dropins`` (no --force) over a hardened DEST: rc=0, 7 drop-ins, 0 base edits.

    RED on the parent commit e8b4eafa: the base templates are added to the write
    set unconditionally, so the preflight refuses (rc=1) and NOT ONE drop-in is
    created; with --force the hardened units themselves are regressed.
    """
    assert len(_HARDENED_B30_LINES) == 60, "the fixture must model the 60-line live unit"

    root = _fake_root(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    units = _unit_names()
    assert len(units) == UNIT_COUNT

    for unit in units:
        (dest / unit).write_text(HARDENED_UNIT_B30, encoding="utf-8")
    before = {unit: _sha256(dest / unit) for unit in units}

    res = subprocess.run(
        _install_cmd(root, "--dest", str(dest), "--dropins"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert res.returncode == 0, (
        f"--dropins without --force must succeed over hardened units:\n{res.stdout}\n{res.stderr}"
    )
    assert "refusing to overwrite" not in res.stderr, f"preflight still refuses:\n{res.stderr}"

    for unit in units:
        dropin = dest / f"{unit}.d" / DROPIN_REL
        assert dropin.is_file(), f"drop-in was not installed: {dropin}"
        text = dropin.read_text(encoding="utf-8")
        assert f"ExecStartPre={root}/scripts/startup_gate.sh" in text, f"gate missing in {dropin}"
        assert "@ANTIGONA_ROOT@" not in text, f"unresolved placeholder in {dropin}"
        leftover = re.findall(r"@ANTIGONA_[A-Z_]+@", text)
        assert not leftover, f"unrendered placeholder token(s) {sorted(set(leftover))} in {dropin}"

    after = {unit: _sha256(dest / unit) for unit in units}
    assert after == before, "a hardened base unit was rewritten by --dropins"
    for unit in units:
        text = (dest / unit).read_text(encoding="utf-8")
        assert text == HARDENED_UNIT_B30, f"{unit} no longer holds the hardened bytes"
        assert "User=antigona-svc" in text, f"{unit} lost the privilege drop"
        assert "ProtectSystem=strict" in text, f"{unit} lost the sandbox"
        assert "ProtectHome=tmpfs" in text, f"{unit} lost ProtectHome"


def test_dropins_only_writes_only_dropins_and_no_base_units(tmp_path: Path) -> None:
    """``--dropins-only`` / ``--no-base-units``: 7 drop-ins, zero base ``*.service``."""
    root = _fake_root(tmp_path)

    for flag in ("--dropins-only", "--no-base-units"):
        dest = tmp_path / f"dest{flag}"
        res = subprocess.run(
            _install_cmd(root, "--dest", str(dest), flag),
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert res.returncode == 0, f"{flag} failed:\n{res.stdout}\n{res.stderr}"

        base_units = sorted(p.name for p in dest.iterdir() if p.suffix == ".service")
        assert base_units == [], f"{flag} wrote base units: {base_units}"

        for unit in _unit_names():
            dropin = dest / f"{unit}.d" / DROPIN_REL
            assert dropin.is_file(), f"{flag}: drop-in not installed: {dropin}"
            assert f"ExecStartPre={root}/scripts/startup_gate.sh" in dropin.read_text(
                encoding="utf-8"
            )
