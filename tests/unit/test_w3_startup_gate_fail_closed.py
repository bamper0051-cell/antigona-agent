"""W3 red/green guard: the live systemd stack must be fail-closed on immutability.

Before this wave the C11 deployment-immutability validator was reachable only from
the manual operator launcher scripts in the deployment checkout; the seven
``deploy/systemd/*.service`` units never invoked it, so for the systemd-managed
live stack the immutability contract was advisory.

The fix ships ``ExecStartPre=@ANTIGONA_ROOT@/scripts/startup_gate.sh`` as a
drop-in (``deploy/systemd/dropins/10-antigona-startup-gate.conf``) that merges
into every unit, and adds ``scripts/startup_gate.sh``, a DEFAULT-CLOSED gate:

* validator rc != 0            -> gate rc != 0  (service start cancelled);
* missing interpreter/module   -> gate rc != 0  (never a silent skip);
* a non-Python override such as ``ANTIGONA_GATE_PYTHON=/bin/true`` (executable,
  exits 0, prints nothing) -> gate rc != 0, loudly refused instead of reporting a
  fake "verified" contract (N1);
* an interpreter that produces no validator output at all -> gate rc != 0
  (no evidence is not a pass);
* ``ANTIGONA_SKIP_VALIDATOR=1`` (a validator-level bypass that returns 0) must NOT
  silently defeat the unit contract -> gate refuses;
* ``ANTIGONA_SKIP_STARTUP_GATE=1`` -> the single, loud, explicit override (rc 0).

B32 (this wave): the interpreter itself must be verified, not merely *named*.  A
stub file called ``python3`` whose whole body is ``echo "startup gate: immutability
contract verified"; exit 0`` passed the basename rule, exited 0 and printed output,
so the gate reported a verified immutability contract while verifying nothing (the
N1 defect class, reproduced independently by two actors).  Before the validator
runs, the gate now probes the interpreter: it must execute REAL Python (version
probe) and must be able to IMPORT ``antigona.startup.validator`` from the candidate
root.  Any mismatch is a loud fail-closed refusal, and there is deliberately no
fallback to a ``python3`` from ``PATH``.  The negative B32 cases below are RED on
the parent commit; the final case is the mandatory CONTROL (a real interpreter on a
genuinely valid tree must still return rc 0, otherwise "refuse everything" would
pass for a fix).

The behavioural cases are black-box: the REAL shell script is copied to a temp
root and driven with a stub interpreter.  The stub lives only in that temp zone
(Security Law 6: mocks are legal inside an explicit test area only); no production
path is mocked.  The tests therefore hold both BEFORE and AFTER a re-freeze of the
C11 envelope — they never assert the real manifest's rc.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
INSTALLER = DEPLOY_SYSTEMD_DIR / "install_units.sh"
GATE_REL = Path("scripts") / "startup_gate.sh"
GATE = REPO_ROOT / GATE_REL
DROPIN = DEPLOY_SYSTEMD_DIR / "dropins" / "10-antigona-startup-gate.conf"
UNIT_COUNT = 7

# The stub interpreter: records the invocation and returns a controlled rc.
#
# B32: it is now probe-aware.  The gate verifies the interpreter (version probe +
# validator-import probe) before it runs the validator, so a double that cannot
# answer those probes would be refused for the wrong reason and every other case in
# this file would fail on an artefact of the double.  It answers both probes exactly
# like real Python and only then falls through to the validator invocation
# controlled by STUB_EXIT_CODE.
STUB_PYTHON = """#!/usr/bin/env bash
case "${1:-}" in
  -c)
    case "${2:-}" in
      *ANTIGONA_GATE_PY_PROBE*) printf 'ANTIGONA_GATE_PY_PROBE py3.11\\n'; exit "${STUB_PROBE_EXIT_CODE:-0}" ;;
      *ANTIGONA_GATE_VALIDATOR_IMPORT_OK*) printf 'ANTIGONA_GATE_VALIDATOR_IMPORT_OK\\n'; exit "${STUB_IMPORT_EXIT_CODE:-0}" ;;
    esac
    exit "${STUB_PROBE_EXIT_CODE:-0}"
    ;;
esac
echo "STUB-VALIDATOR-INVOKED $*"
exit "${STUB_EXIT_CODE:-0}"
"""


def _service_files() -> list[Path]:
    files = sorted(DEPLOY_SYSTEMD_DIR.glob("*.service"))
    assert len(files) == UNIT_COUNT, f"expected {UNIT_COUNT} units, found {len(files)}"
    return files


def _exec_start_pre_lines(unit: Path) -> list[str]:
    return [
        line.strip()
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("ExecStartPre=")
    ]


# ── static contract: every unit gates the start, the gate script is shippable ──


def test_every_unit_has_exactly_one_gate_exec_start_pre() -> None:
    """The gate is declared exactly once per unit, and it comes from the drop-in.

    B29 supersedes the B28 shape of this test.  The seven live units in
    /etc/systemd/system are HARDENED units with NO ExecStartPre (observed
    2026-09-16: ``grep -l ExecStartPre /etc/systemd/system/antigona-*.service``
    -> rc=1, no output), and the shipped deploy/systemd/*.service templates are
    now the faithful render of those live units.  Declaring the gate in the base
    unit AND installing the drop-in would run it twice per start, so the gate
    lives in exactly one place: the drop-in that merges into an
    already-hardened unit without replacing it.
    """
    expected = "ExecStartPre=@ANTIGONA_ROOT@/scripts/startup_gate.sh"
    for unit in _service_files():
        directives = _exec_start_pre_lines(unit)
        assert directives == [], (
            f"{unit.name}: the hardened base unit must not carry the gate "
            f"(it is delivered by the drop-in); got {directives}"
        )
    assert DROPIN.is_file(), f"gate drop-in missing: {DROPIN}"
    assert _exec_start_pre_lines(DROPIN) == [expected], (
        f"the drop-in must declare exactly one gate ExecStartPre, got "
        f"{_exec_start_pre_lines(DROPIN)}"
    )


def test_gate_script_is_present_executable_and_syntax_valid() -> None:
    """The gate script ships executable and passes ``bash -n``."""
    assert GATE.is_file(), f"gate script missing: {GATE}"
    mode = GATE.stat().st_mode
    assert mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), f"{GATE} is not executable"

    res = subprocess.run(["bash", "-n", str(GATE)], capture_output=True, text=True)
    assert res.returncode == 0, f"bash -n failed for {GATE}:\n{res.stderr}"


def test_gate_script_has_no_hardcoded_home_path() -> None:
    """Portability: the gate derives its root, it never hardcodes a home literal.

    Matches the project-wide home-derivation wave (ADR-007/B22): the shell gate
    must stay correct for any deployment user and any checkout directory.
    """
    text = GATE.read_text(encoding="utf-8")
    assert "/opt/antigona-home" not in text, "gate script must not hardcode the canonical home literal"
    assert "/home/" not in text, "gate script must not hardcode a /home/<user> path"
    assert "ANTIGONA_GATE_ROOT" in text, "gate must expose an explicit root override"


def test_installer_dry_run_renders_resolvable_gate_path() -> None:
    """install_units.sh --dry-run resolves the gate path and leaves no placeholder.

    The rendered path must exist on disk for the rendered root, so a unit cannot
    be installed pointing at a gate that was not shipped.
    """
    resolved_root = REPO_ROOT.resolve()
    res = subprocess.run(
        [str(INSTALLER), str(resolved_root), "--dry-run", "--dropins"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"dry-run failed:\n{res.stderr}"
    output = res.stdout
    assert "@ANTIGONA_ROOT@" not in output, "leftover @ANTIGONA_ROOT@ in rendered units"

    rendered = f"ExecStartPre={resolved_root}/scripts/startup_gate.sh"
    assert rendered in output, f"rendered gate directive missing from dry-run output: {rendered}"
    assert output.count(rendered) == UNIT_COUNT, (
        f"expected {UNIT_COUNT} rendered gate directives, found {output.count(rendered)}"
    )
    assert (resolved_root / GATE_REL).is_file(), "rendered gate path does not exist"


# ── behavioural contract: default-closed, black-box, real bash ─────────────────


def _make_fake_root(tmp_path: Path) -> Path:
    """Build a temp candidate root with a copy of the real gate + a stub python."""
    root = tmp_path / "fake-root"
    (root / "src" / "antigona" / "startup").mkdir(parents=True)
    (root / "src" / "antigona" / "startup" / "validator.py").write_text(
        "# stub validator module (test zone only)\n", encoding="utf-8"
    )
    (root / "scripts").mkdir()
    shutil.copy2(GATE, root / GATE_REL)
    (root / GATE_REL).chmod(0o755)

    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True)
    stub = bindir / "python"
    stub.write_text(STUB_PYTHON, encoding="utf-8")
    stub.chmod(0o755)
    return root


def _run_gate(root: Path, *, stub_exit: int | None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the copied gate and return the completed process.

    ``stub_exit is None`` removes the interpreter so the missing-python path runs.
    The ambient ``ANTIGONA_*`` overrides are stripped: every case controls exactly
    the environment it claims to exercise.
    """
    env = dict(os.environ)
    for key in (
        "ANTIGONA_SKIP_VALIDATOR",
        "ANTIGONA_SKIP_STARTUP_GATE",
        "ANTIGONA_GATE_ROOT",
        "ANTIGONA_GATE_PYTHON",
        "ANTIGONA_GATE_CHECK",
    ):
        env.pop(key, None)
    if stub_exit is None:
        (root / ".venv" / "bin" / "python").unlink()
    else:
        env["STUB_EXIT_CODE"] = str(stub_exit)
    env.update(extra_env or {})

    return subprocess.run(
        ["bash", str(root / GATE_REL)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
    )


def test_gate_is_default_closed_when_validator_fails(tmp_path: Path) -> None:
    """Validator rc != 0 => gate rc != 0 and a CRITICAL message on stderr."""
    root = _make_fake_root(tmp_path)
    res = _run_gate(root, stub_exit=1)

    assert res.returncode != 0, f"gate must be fail-closed, got rc={res.returncode}"
    assert "CRITICAL" in res.stderr, f"no CRITICAL banner:\n{res.stderr}"
    assert "FAIL-CLOSED" in res.stderr, f"no FAIL-CLOSED marker:\n{res.stderr}"
    assert "STUB-VALIDATOR-INVOKED" in res.stdout, f"gate did not run the validator:\n{res.stdout}"


def test_gate_passes_and_runs_validator_when_validator_succeeds(tmp_path: Path) -> None:
    """Validator rc == 0 => the gate reports success and returns 0."""
    root = _make_fake_root(tmp_path)
    res = _run_gate(root, stub_exit=0)

    assert res.returncode == 0, f"gate should pass, got rc={res.returncode}\n{res.stderr}"
    assert "STUB-VALIDATOR-INVOKED" in res.stdout, f"validator was not invoked:\n{res.stdout}"
    assert "--check=manifest" in res.stdout, (
        f"gate must default to the manifest phase:\n{res.stdout}"
    )


def test_gate_fails_closed_when_interpreter_is_missing(tmp_path: Path) -> None:
    """No interpreter => fail-closed, never a silent skip."""
    root = _make_fake_root(tmp_path)
    res = _run_gate(root, stub_exit=None)

    assert res.returncode != 0, "missing interpreter must be fail-closed"
    assert "no executable interpreter" in res.stderr, f"unclear refusal:\n{res.stderr}"


def test_gate_refuses_validator_level_bypass(tmp_path: Path) -> None:
    """``ANTIGONA_SKIP_VALIDATOR=1`` must not silently disable the unit contract.

    The validator honours that variable by printing "пропущен" and returning 0, so
    without this refusal the gate would pass on an unverified candidate.
    """
    root = _make_fake_root(tmp_path)
    res = _run_gate(root, stub_exit=0, extra_env={"ANTIGONA_SKIP_VALIDATOR": "1"})

    assert res.returncode != 0, "the validator-level bypass must be refused by the gate"
    assert "ANTIGONA_SKIP_VALIDATOR" in res.stderr, f"unclear refusal:\n{res.stderr}"
    assert "STUB-VALIDATOR-INVOKED" not in res.stdout, "the validator must not have been run"


def test_gate_explicit_override_passes_loudly(tmp_path: Path) -> None:
    """``ANTIGONA_SKIP_STARTUP_GATE=1`` is the single, loud override (rc 0)."""
    root = _make_fake_root(tmp_path)
    res = _run_gate(root, stub_exit=1, extra_env={"ANTIGONA_SKIP_STARTUP_GATE": "1"})

    assert res.returncode == 0, f"override must bypass the gate, got rc={res.returncode}"
    assert "ANTIGONA_SKIP_STARTUP_GATE=1" in res.stderr, f"override is not loud:\n{res.stderr}"
    assert "WARNING" in res.stderr, f"override must warn:\n{res.stderr}"


def test_gate_root_override_is_honoured(tmp_path: Path) -> None:
    """``ANTIGONA_GATE_ROOT`` redirects the gate (needed for out-of-tree tests)."""
    root = _make_fake_root(tmp_path)
    elsewhere = tmp_path / "gate-copy-dir"
    elsewhere.mkdir()
    detached = elsewhere / "startup_gate.sh"
    shutil.copy2(GATE, detached)
    detached.chmod(0o755)

    res = subprocess.run(
        ["bash", str(detached)],
        capture_output=True,
        text=True,
        env={
            **{k: v for k, v in os.environ.items() if k != "ANTIGONA_SKIP_VALIDATOR"},
            "ANTIGONA_GATE_ROOT": str(root),
            "ANTIGONA_GATE_PYTHON": str(root / ".venv" / "bin" / "python"),
            "ANTIGONA_GATE_CHECK": "pre",
            "STUB_EXIT_CODE": "0",
        },
        cwd=str(tmp_path),
    )
    assert res.returncode == 0, f"explicit root override failed:\n{res.stderr}"
    assert "--check=pre" in res.stdout, f"check override ignored:\n{res.stdout}"


# ── N1: an unverifiable interpreter must never fake a verified contract ───────


def test_gate_fail_closed_on_non_python_interpreter(tmp_path: Path) -> None:
    """``ANTIGONA_GATE_PYTHON=/bin/true`` must be refused loudly, not passed silently.

    RED before the fix: the gate ran ``/bin/true -m antigona.startup.validator
    --check=manifest``, which exits 0 and prints nothing, and reported
    ``immutability contract verified`` with rc=0 (measured on e8b4eafa: rc=0,
    stderr 0 bytes).
    """
    root = _make_fake_root(tmp_path)
    res = _run_gate(root, stub_exit=0, extra_env={"ANTIGONA_GATE_PYTHON": "/bin/true"})

    assert res.returncode != 0, "a non-Python interpreter must be fail-closed"
    assert "CRITICAL" in res.stderr, f"no CRITICAL banner:\n{res.stderr}"
    assert "FAIL-CLOSED" in res.stderr, f"no FAIL-CLOSED marker:\n{res.stderr}"
    assert "/bin/true" in res.stderr, f"the refused interpreter is not named:\n{res.stderr}"
    assert "immutability contract verified" not in res.stdout, (
        f"the gate faked a verified contract:\n{res.stdout}"
    )


def test_gate_fail_closed_on_silent_python_named_interpreter(tmp_path: Path) -> None:
    """A silent interpreter that *is* named ``python`` is refused too (no evidence)."""
    root = _make_fake_root(tmp_path)
    silent = root / ".venv" / "bin" / "python"
    silent.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    silent.chmod(0o755)

    res = _run_gate(root, stub_exit=0)

    assert res.returncode != 0, "a silent interpreter must be fail-closed"
    assert "CRITICAL" in res.stderr, f"no CRITICAL banner:\n{res.stderr}"
    assert "immutability contract verified" not in res.stdout, (
        f"the gate faked a verified contract on empty validator output:\n{res.stdout}"
    )


# ── B32: the interpreter itself must be REAL Python that reaches the validator ─
#
# Naming a Python interpreter is not verifying one.  The reproduced B32 stub is two
# lines long and is *called* ``python3``:

B32_DEFECT_STUB = """#!/bin/sh
echo "startup gate: immutability contract verified"
exit 0
"""

# A stub that DOES answer the interpreter probes exactly like real Python, so a
# refusal proves the gate reads the probe RESULT (not merely the file name).  The
# validator-import probe deliberately answers with text that is not the marker.
B32_PROBE_AWARE_STUB = """#!/bin/sh
if [ "$1" = "-c" ]; then
    case "$2" in
        *ANTIGONA_GATE_PY_PROBE*) echo "ANTIGONA_GATE_PY_PROBE py3.11"; exit "${STUB_PROBE_EXIT_CODE:-0}" ;;
        *ANTIGONA_GATE_VALIDATOR_IMPORT_OK*) echo "STUB-IMPORT-NOT-THE-MARKER"; exit "${STUB_IMPORT_EXIT_CODE:-0}" ;;
    esac
    exit "${STUB_PROBE_EXIT_CODE:-0}"
fi
echo "STUB-VALIDATOR-INVOKED $*"
exit "${STUB_EXIT_CODE:-0}"
"""

# A test-zone validator double, reachable only if the interpreter's environment does
# not already ship the real package; it prints a report line and exits 0, so the
# control below measures the GATE (Security Law 6: doubles live in the test zone).
VALIDATOR_DOUBLE = """# test-zone validator double (never a production path)
print("=== test-zone validator double ===")
print("stub check: ok")
"""

B32_INTERPRETERS = "interpreters"


def _write_stub(directory: Path, name: str, content: str) -> Path:
    """Write an executable stub interpreter inside a temp zone (never in the repo)."""
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / name
    stub.write_text(content, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _candidate_root(tmp_path: Path) -> Path:
    """Temp candidate root holding a real copy of the gate and the validator file."""
    root = tmp_path / "b32-root"
    (root / "src" / "antigona" / "startup").mkdir(parents=True)
    (root / "src" / "antigona" / "startup" / "validator.py").write_text(
        VALIDATOR_DOUBLE, encoding="utf-8"
    )
    (root / "scripts").mkdir()
    shutil.copy2(GATE, root / GATE_REL)
    (root / GATE_REL).chmod(0o755)
    return root


def _run_gate_with_root(
    root: Path, interpreter: Path, gate: Path | None = None, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    """Drive a gate script with an explicit interpreter; strip ambient overrides.

    ``gate`` defaults to the copy inside ``root``; the valid-tree control passes the
    canonical script instead, because adding it to that tree would be an unlisted
    file and would (correctly) make the manifest scan fail.
    """
    script = gate if gate is not None else root / GATE_REL
    env = {key: value for key, value in os.environ.items() if not key.startswith("ANTIGONA_")}
    env["ANTIGONA_GATE_ROOT"] = str(root)
    env["ANTIGONA_GATE_PYTHON"] = str(interpreter)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
    )


def _repository_interpreter() -> Path:
    """The candidate's own venv interpreter (the running interpreter otherwise)."""
    candidate = REPO_ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else Path(sys.executable)


def _valid_candidate_tree(tmp_path: Path) -> Path:
    """A candidate root whose C11 manifest genuinely authenticates (schema v2).

    Minimal, but real: the manifest, its report and the listed hashes follow the
    production contract exactly, so the REAL interpreter plus the REAL validator
    return rc 0 on it without mocking any production path.
    """
    root = tmp_path / "valid-root"
    (root / "src" / "antigona" / "startup").mkdir(parents=True)
    files: dict[str, str] = {}
    for rel, content in (
        ("app.py", "immutable\n"),
        ("src/antigona/startup/validator.py", VALIDATOR_DOUBLE),
    ):
        (root / rel).write_text(content, encoding="utf-8")
        files[rel] = hashlib.sha256(content.encode("utf-8")).hexdigest()

    commit = "e03c909923e517bdcfc818df48950e808a808e08"
    base: dict[str, object] = {
        "schema": "antigona-deployment-manifest/v2",
        "mode": "gitless",
        "commit": commit,
        "source_commit": commit,
        "release_metadata_commit": commit,
        "provenance": {"source_commit": commit},
        "file_count": len(files),
        "scope": "explicit test zone; no production path is described by this manifest",
        "files": files,
    }
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    report = root / "CANDIDATE_MANIFEST_HASH_REPORT.md"
    report.write_text(
        json.dumps(
            {
                "schema": "antigona-manifest-report/v1",
                "mode": "gitless",
                "commit": commit,
                "source_commit": commit,
                "release_metadata_commit": commit,
                "file_count": len(files),
                "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    base.update(
        report="CANDIDATE_MANIFEST_HASH_REPORT.md",
        report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
    )
    (root / "CANDIDATE_DEPLOYMENT_MANIFEST.json").write_text(
        json.dumps(base), encoding="utf-8"
    )
    return root


def test_gate_fail_closed_on_python3_named_stub_that_only_echoes_success(
    tmp_path: Path,
) -> None:
    """B32 regression: a ``python3`` stub echoing the success line must be refused.

    RED on the parent commit: the stub passed the basename rule, exited 0, printed
    the success line and the gate returned rc=0 with stdout
    ``startup gate: immutability contract verified`` — a verified contract for a
    verification that never happened.
    """
    root = _candidate_root(tmp_path)
    stub = _write_stub(tmp_path / B32_INTERPRETERS, "python3", B32_DEFECT_STUB)

    res = _run_gate_with_root(root, stub)

    assert res.returncode != 0, (
        f"a stub interpreter named python3 must be fail-closed, got rc={res.returncode}:"
        f"\n{res.stdout}"
    )
    assert "CRITICAL" in res.stderr, f"no CRITICAL banner:\n{res.stderr}"
    assert "FAIL-CLOSED" in res.stderr, f"no FAIL-CLOSED marker:\n{res.stderr}"
    assert "immutability contract verified" not in res.stdout, (
        f"the gate faked a verified contract:\n{res.stdout}"
    )
    assert "STUB-VALIDATOR-INVOKED" not in res.stdout, (
        f"the gate ran the validator with an unverified interpreter:\n{res.stdout}"
    )


def test_gate_fail_closed_when_version_probe_fails(tmp_path: Path) -> None:
    """A ``python3`` stub that answers the version probe with rc != 0 is refused.

    RED on the parent commit: no version probe existed, so the stub's validator
    invocation exited 0 and the gate reported a verified contract.
    """
    root = _candidate_root(tmp_path)
    stub = _write_stub(tmp_path / B32_INTERPRETERS, "python3", B32_PROBE_AWARE_STUB)

    res = _run_gate_with_root(root, stub, STUB_PROBE_EXIT_CODE="1")

    assert res.returncode != 0, f"a failing version probe must be fail-closed:\n{res.stdout}"
    assert "CRITICAL" in res.stderr, f"no CRITICAL banner:\n{res.stderr}"
    assert "FAIL-CLOSED" in res.stderr, f"no FAIL-CLOSED marker:\n{res.stderr}"
    assert "immutability contract verified" not in res.stdout, (
        f"the gate faked a verified contract:\n{res.stdout}"
    )


def test_gate_fail_closed_when_validator_import_probe_fails(tmp_path: Path) -> None:
    """An interpreter that cannot import the validator is refused (rc != 0 probe)."""
    root = _candidate_root(tmp_path)
    stub = _write_stub(tmp_path / B32_INTERPRETERS, "python3", B32_PROBE_AWARE_STUB)

    res = _run_gate_with_root(root, stub, STUB_IMPORT_EXIT_CODE="1")

    assert res.returncode != 0, (
        f"an interpreter that cannot reach the validator must be fail-closed:\n{res.stdout}"
    )
    assert "CRITICAL" in res.stderr, f"no CRITICAL banner:\n{res.stderr}"
    assert "FAIL-CLOSED" in res.stderr, f"no FAIL-CLOSED marker:\n{res.stderr}"
    assert "antigona.startup.validator" in res.stderr, (
        f"the unreachable module is not named:\n{res.stderr}"
    )
    assert "immutability contract verified" not in res.stdout, (
        f"the gate faked a verified contract:\n{res.stdout}"
    )


def test_gate_fail_closed_on_import_probe_without_marker_output(tmp_path: Path) -> None:
    """Import probe rc 0 but wrong output must not count as evidence either."""
    root = _candidate_root(tmp_path)
    stub = _write_stub(tmp_path / B32_INTERPRETERS, "python3", B32_PROBE_AWARE_STUB)

    res = _run_gate_with_root(root, stub, STUB_IMPORT_EXIT_CODE="0")

    assert res.returncode != 0, (
        f"a non-empty but markerless import probe must be fail-closed:\n{res.stdout}"
    )
    assert "CRITICAL" in res.stderr, f"no CRITICAL banner:\n{res.stderr}"
    assert "FAIL-CLOSED" in res.stderr, f"no FAIL-CLOSED marker:\n{res.stderr}"
    assert "STUB-IMPORT-NOT-THE-MARKER" in res.stderr, (
        f"the probe output is not shown:\n{res.stderr}"
    )
    assert "immutability contract verified" not in res.stdout, (
        f"the gate faked a verified contract:\n{res.stdout}"
    )


def test_gate_control_passes_with_real_interpreter_on_valid_tree(tmp_path: Path) -> None:
    """CONTROL: a real interpreter on a valid tree still returns rc=0.

    Mandatory companion to the refusals above: without it a gate that simply
    refused every interpreter would satisfy them.  The tree is minimal but its C11
    manifest genuinely authenticates, so the real interpreter and the real
    validator walk the production success path.
    """
    interpreter = _repository_interpreter()
    assert interpreter.is_file(), f"no real interpreter available for the control: {interpreter}"
    assert Path(interpreter).name.startswith("python"), interpreter

    root = _valid_candidate_tree(tmp_path)
    res = _run_gate_with_root(root, interpreter, gate=GATE)

    assert res.returncode == 0, (
        f"a real interpreter on a valid tree must pass, got rc={res.returncode}:"
        f"\n{res.stdout}\n{res.stderr}"
    )
    assert "startup gate: immutability contract verified" in res.stdout, (
        f"the explicit verified line is missing:\n{res.stdout}"
    )
    assert "FAIL-CLOSED" not in res.stderr, f"the control was refused:\n{res.stderr}"
