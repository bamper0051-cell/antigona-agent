"""B29 RED-FIRST falsifier: the shipped systemd templates must be the source of
truth for the HARDENED production units, not the 18-line legacy launchers.

Background
----------
Before B29 the repository shipped seven 18-line legacy templates
(``WorkingDirectory=@ANTIGONA_ROOT@`` + ``ExecStart=.../service_wrapper.sh``),
while the seven units actually RUNNING in ``/etc/systemd/system`` are 58-61-line
HARDENED units:

* ``User=antigona-svc`` / ``Group=antigona-svc`` (privileged drop),
* ``ProtectSystem=strict`` + ``ProtectHome=tmpfs`` (sandbox),
* ``NoNewPrivileges=yes``, ``CapabilityBoundingSet=``,
* ``ReadWritePaths=`` / ``BindReadOnlyPaths=``,
* per-service ``Environment=ANTIGONA_*`` policy.

A fresh install from the published artifact therefore could not reproduce the
privileged drop + sandbox at all, and ``install_units.sh --check`` compared the
live hardened units against the legacy templates and reported all seven as
``DRIFTED`` (proven in the B29 audit: rc=1, seven DRIFTED lines, zero writes).

These tests read the repository only.  No ``/etc`` write, no ``systemctl``, no
``sudo``.  The live-parity test skips when the live units are absent instead of
failing, so the suite stays runnable on a machine that is not the deployment
host.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
INSTALLER = DEPLOY_SYSTEMD_DIR / "install_units.sh"
LIVE_UNIT_DIR = Path("/etc/systemd/system")

# The install root the templates are expected to render to.  Defaults to this
# checkout, which is correct on the deployment host; an off-host verification run
# can pin the real install root with ANTIGONA_EXPECTED_ROOT.
EXPECTED_ROOT = os.environ.get("ANTIGONA_EXPECTED_ROOT", str(REPO_ROOT))
# The installing user's home; @ANTIGONA_ENV_FILE@ / @ANTIGONA_UV_PYTHON@ render
# under it.  Mirrors install_units.sh: $ANTIGONA_HOME_DIR wins, else $HOME.
EXPECTED_HOME = os.environ.get(
    "ANTIGONA_EXPECTED_HOME",
    os.environ.get("ANTIGONA_HOME_DIR") or os.environ.get("HOME") or os.path.expanduser("~"),
)

UNIT_NAMES = (
    "antigona-bot.service",
    "antigona-delivery.service",
    "antigona-docker-proxy.service",
    "antigona-gateway.service",
    "antigona-orchestration.service",
    "antigona-verifier.service",
    "antigona-worker.service",
)

# Directives that make the unit hardened.  A '# ' comment line must not satisfy
# them, so matching is anchored to the beginning of a non-comment line.
REQUIRED_HARDENING = (
    "User=",
    "Group=",
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",
    "ProtectHome=",
    "CapabilityBoundingSet=",
    "SystemCallArchitectures=native",
    "ReadWritePaths=",
    "BindReadOnlyPaths=",
)

ANTIGONA_ROOT_TOKEN = "@ANTIGONA_ROOT@"
# B34: tokenized home-dependent paths (see deploy/systemd/install_units.sh).
ANTIGONA_ENV_TOKEN = "@ANTIGONA_ENV_FILE@"
ANTIGONA_UV_PYTHON_TOKEN = "@ANTIGONA_UV_PYTHON@"

# The B29 acceptance criterion, verbatim.
BARE_ROOT_LITERAL = re.compile(r"/opt/antigona-home($|[^/a-zA-Z0-9_])")


def _directive_lines(text: str) -> list[str]:
    """Non-comment, non-blank lines of a unit file, indentation stripped."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        out.append(stripped)
    return out


def _render(text: str) -> str:
    """Substitute every placeholder exactly the way install_units.sh does."""
    return (
        text.replace(ANTIGONA_ROOT_TOKEN, EXPECTED_ROOT)
        .replace(ANTIGONA_ENV_TOKEN, f"{EXPECTED_HOME}/antigona.env")
        .replace(ANTIGONA_UV_PYTHON_TOKEN, f"{EXPECTED_HOME}/.local/share/uv/python")
    )


def test_shipped_templates_are_hardened() -> None:
    """Every shipped *.service template carries the sandbox and privilege drop.

    RED on the pre-B29 tree: the legacy templates have none of these directives.
    """
    for name in UNIT_NAMES:
        unit = DEPLOY_SYSTEMD_DIR / name
        assert unit.is_file(), f"missing shipped template: {unit}"
        lines = _directive_lines(unit.read_text(encoding="utf-8"))
        present = set()
        for directive in REQUIRED_HARDENING:
            if any(line.startswith(directive) for line in lines):
                present.add(directive)
        assert present == set(REQUIRED_HARDENING), (
            f"{name} is not a hardened template; missing "
            f"{sorted(set(REQUIRED_HARDENING) - present)}"
        )
        assert len(lines) >= 40, (
            f"{name}: only {len(lines)} directive lines; the hardened live units "
            f"carry 51-61 lines total, so this looks like a legacy launcher"
        )


def test_shipped_templates_drop_privileges_to_a_real_service_user() -> None:
    """User=/Group= name the dedicated service account, never root."""
    for name in UNIT_NAMES:
        lines = _directive_lines((DEPLOY_SYSTEMD_DIR / name).read_text(encoding="utf-8"))
        user = [line for line in lines if line.startswith("User=")]
        assert user == ["User=antigona-svc"], f"{name}: expected User=antigona-svc, got {user}"
        group = [line for line in lines if line.startswith("Group=")]
        assert group == ["Group=antigona-svc"], f"{name}: expected Group=antigona-svc, got {group}"


def test_shipped_templates_carry_no_bare_root_literal() -> None:
    """No shipped template hardcodes the owner's home directory.

    A bare '/opt/antigona-home' (end of line, or followed by anything but a path segment)
    would make the template non-portable; the only permitted /opt/antigona-home paths are the
    out-of-tree secret file and the uv interpreter bind, which are full paths,
    not a bare home reference.
    """
    for name in UNIT_NAMES:
        text = (DEPLOY_SYSTEMD_DIR / name).read_text(encoding="utf-8")
        for idx, line in enumerate(text.splitlines(), start=1):
            match = BARE_ROOT_LITERAL.search(line)
            assert match is None, f"{name}:{idx}: bare '/opt/antigona-home' literal: {line.strip()}"


def test_shipped_templates_use_the_root_placeholder() -> None:
    """Install-tree paths are parameterised with @ANTIGONA_ROOT@."""
    for name in UNIT_NAMES:
        text = (DEPLOY_SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert ANTIGONA_ROOT_TOKEN in text, f"{name}: no {ANTIGONA_ROOT_TOKEN} placeholder"
        lines = _directive_lines(text)
        for directive in ("WorkingDirectory=", "ExecStart=", "BindReadOnlyPaths="):
            values = [line for line in lines if line.startswith(directive)]
            assert values, f"{name}: missing {directive.rstrip('=')}"
            for value in values:
                assert ANTIGONA_ROOT_TOKEN in value, (
                    f"{name}: {directive} does not use {ANTIGONA_ROOT_TOKEN}: {value}"
                )


def test_shipped_templates_have_no_execstartpre() -> None:
    """The fail-closed startup gate arrives as a drop-in, not in the unit.

    The seven live hardened units have no ExecStartPre; the gate is delivered by
    deploy/systemd/dropins/10-antigona-startup-gate.conf so that it can be merged
    into an already-hardened unit without replacing it.
    """
    for name in UNIT_NAMES:
        lines = _directive_lines((DEPLOY_SYSTEMD_DIR / name).read_text(encoding="utf-8"))
        gate = [line for line in lines if line.startswith("ExecStartPre=")]
        assert gate == [], f"{name}: ExecStartPre must live in the drop-in, found {gate}"


def test_shipped_templates_declare_the_production_env_file() -> None:
    """Every shipped template still reads the production env file.

    Preserves the committed env-parity invariant
    (tests/unit/test_systemd_units_env_parity.py) across the B29 rewrite: the
    out-of-tree secret file is referenced through the @ANTIGONA_ENV_FILE@ token
    (B34) so the published template carries no absolute home literal; the
    installer renders the token to the live host path, keeping the live units
    byte-identical.
    """
    for name in UNIT_NAMES:
        text = (DEPLOY_SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert "EnvironmentFile=" in text, f"{name}: no EnvironmentFile="
        assert ANTIGONA_ENV_TOKEN in text, (
            f"{name}: EnvironmentFile must reference the production env file token "
            f"{ANTIGONA_ENV_TOKEN}"
        )


def test_legacy_templates_are_preserved_outside_the_service_glob() -> None:
    """The pre-B29 legacy launchers survive as *.legacy.template (not installed)."""
    for name in UNIT_NAMES:
        legacy = DEPLOY_SYSTEMD_DIR / f"{name}.legacy.template"
        assert legacy.is_file(), f"legacy template not preserved: {legacy}"
        lines = _directive_lines(legacy.read_text(encoding="utf-8"))
        assert any(line.startswith("ExecStart=") for line in lines), (
            f"{legacy.name} is not a launcher"
        )
        if name != "antigona-docker-proxy.service":
            # Six of the seven pre-B29 templates were un-hardened 18-line legacy
            # launchers; docker-proxy was the single already-hardened exception.
            assert not any(line.startswith("ProtectSystem=") for line in lines), (
                f"{legacy.name} should be the un-hardened legacy launcher"
            )
    installed = sorted(p.name for p in DEPLOY_SYSTEMD_DIR.glob("*.service"))
    assert installed == sorted(UNIT_NAMES), (
        f"the installer glob must see exactly the seven hardened units, saw {installed}"
    )


def test_installer_refuses_a_non_hardened_template_set(tmp_path: Path) -> None:
    """The installer fails closed if a template is reverted to a legacy render.

    Black-box: a fake install tree (Security Law 6, explicit test zone) is built
    with the legacy bytes and the installer must refuse to write it.
    """
    assert INSTALLER.is_file()
    fake_root = tmp_path / "root"
    (fake_root / "src" / "antigona").mkdir(parents=True)
    legacy = (DEPLOY_SYSTEMD_DIR / "antigona-gateway.service.legacy.template").read_text(
        encoding="utf-8"
    )
    script_dir = tmp_path / "systemd"
    script_dir.mkdir()
    (script_dir / "antigona-gateway.service").write_text(legacy, encoding="utf-8")
    # Copy the real installer next to the reverted template so it discovers it.
    (script_dir / "install_units.sh").write_bytes(INSTALLER.read_bytes())
    res = subprocess.run(
        ["bash", str(script_dir / "install_units.sh"), str(fake_root),
         "--dest", str(tmp_path / "dest")],
        capture_output=True, text=True,
    )
    assert res.returncode != 0, (
        f"installer accepted a non-hardened template set:\n{res.stdout}\n{res.stderr}"
    )
    assert "hardening" in res.stderr.lower(), res.stderr
    assert not (tmp_path / "dest" / "antigona-gateway.service").exists(), (
        "a non-hardened unit was written despite the guard"
    )


@pytest.mark.parametrize("name", UNIT_NAMES)
def test_rendered_template_is_byte_identical_to_the_live_hardened_unit(name: str) -> None:
    """The shipped template, with @ANTIGONA_ROOT@ substituted, IS the live unit.

    This is the B29 source-of-truth claim, stated as an executable equality rather
    than prose.  It skips (does not fail) when the live deployment is not present.
    """
    live = LIVE_UNIT_DIR / name
    if not live.is_file():
        pytest.skip(f"live unit not present: {live}")
    if EXPECTED_ROOT not in live.read_text(encoding="utf-8"):
        pytest.skip(
            f"{name}: the live unit does not install from {EXPECTED_ROOT}, so byte "
            f"parity is meaningless here (set ANTIGONA_EXPECTED_ROOT on the host)"
        )
    template = (DEPLOY_SYSTEMD_DIR / name).read_text(encoding="utf-8")
    assert _render(template).encode("utf-8") == live.read_bytes(), (
        f"{name}: shipped template is not byte-identical to the live unit after "
        f"@ANTIGONA_ROOT@ substitution"
    )


def test_live_hardened_units_have_no_execstartpre() -> None:
    """Read-only observation of the live tree: the gate is not yet in the units."""
    present = sorted(p.name for p in LIVE_UNIT_DIR.glob("antigona-*.service"))
    if not present:
        pytest.skip("no live antigona units on this machine")
    for name in present:
        text = (LIVE_UNIT_DIR / name).read_text(encoding="utf-8")
        lines = _directive_lines(text)
        assert not any(line.startswith("ExecStartPre=") for line in lines), (
            f"{name}: live unit carries an ExecStartPre; the startup-gate drop-in "
            f"may already have been activated (owner-gated action)"
        )


# ── B34 RED-FIRST falsifier: no absolute-home literal in the published tree ───
#
# The frozen release pipeline REFUSES to rewrite deploy/** (H1/R18) and then
# fails the build on any surviving '/opt/antigona-home/' literal (the FORBIDDEN gate), and a
# scoped gate forbids a re-introduced '/home/<user>/' literal in functional code.
# The shipped systemd artifacts must therefore carry NO absolute-home literal at
# all: every home-dependent path is one of the @ANTIGONA_*@ tokens the installer
# renders.  The needles are assembled from fragments so this detector is not
# itself a '/opt/antigona-home/' or '/home/' literal in the built (public) tree.
ABSOLUTE_HOME_TOKENS = (
    "/" + "root" + "/",
    "/" + "home" + "/",
)

PUBLISHED_UNIT_FILES = tuple(UNIT_NAMES) + tuple(f"{name}.legacy.template" for name in UNIT_NAMES)


def test_published_systemd_artifacts_have_no_absolute_home_literal() -> None:
    """RED on the parent commit 129a371b, GREEN once the literals are tokenized.

    On 129a371b every one of the seven *.service and seven *.legacy.template files
    still carries '/etc/antigona/antigona.env' (and the six hardened units also
    '/opt/antigona-home/.local/share/uv/python'), which is exactly what fails the frozen build.
    Only functional (non-comment, non-blank) lines are scanned: a comment may
    document the operator-facing path without shipping it as a directive.
    """
    scanned = 0
    for name in PUBLISHED_UNIT_FILES:
        path = DEPLOY_SYSTEMD_DIR / name
        assert path.is_file(), f"missing published artifact: {path}"
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            scanned += 1
            for token in ABSOLUTE_HOME_TOKENS:
                assert token not in line, (
                    f"{name}:{idx}: absolute-home literal {token!r} in a published "
                    f"functional line: {stripped}"
                )
    assert scanned > 0, "no functional lines scanned; the artifact set looks wrong"


def test_installer_renders_every_tokenized_placeholder(tmp_path: Path) -> None:
    """install_units.sh must substitute ALL @ANTIGONA_*@ tokens, not just ROOT.

    Black-box against the real installer in a fake tree (explicit test zone).
    RED on 129a371b: @ANTIGONA_ENV_FILE@ / @ANTIGONA_UV_PYTHON@ are unknown to the
    renderer, so they survive into the rendered unit -- and the pre-B34 guard,
    which only looks for @ANTIGONA_ROOT@, does not catch them.
    """
    assert INSTALLER.is_file()
    fake_root = tmp_path / "candidate-root"
    (fake_root / "src" / "antigona").mkdir(parents=True)
    scripts = tmp_path / "systemd"
    scripts.mkdir()
    (scripts / "install_units.sh").write_bytes(INSTALLER.read_bytes())
    synthetic = (
        "[Unit]\n"
        "Description=synthetic placeholder probe\n\n"
        "[Service]\n"
        "User=antigona-svc\n"
        "Group=antigona-svc\n"
        "EnvironmentFile=-@ANTIGONA_ENV_FILE@\n"
        "WorkingDirectory=@ANTIGONA_ROOT@\n"
        "ExecStart=@ANTIGONA_ROOT@/.venv/bin/python -c 'pass'\n"
        "NoNewPrivileges=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=tmpfs\n"
        "CapabilityBoundingSet=\n"
        "SystemCallArchitectures=native\n"
        "ReadWritePaths=/run/antigona\n"
        "BindReadOnlyPaths=@ANTIGONA_ROOT@ @ANTIGONA_UV_PYTHON@ @ANTIGONA_ENV_FILE@\n"
    )
    (scripts / "antigona-synthetic.service").write_text(synthetic, encoding="utf-8")

    home = tmp_path / "fake-home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    env.pop("ANTIGONA_HOME_DIR", None)  # force the $HOME branch of the derivation
    res = subprocess.run(
        ["bash", str(scripts / "install_units.sh"), str(fake_root), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"dry-run failed:\n{res.stdout}\n{res.stderr}"
    assert f"{home}/antigona.env" in res.stdout, res.stdout
    assert f"{home}/.local/share/uv/python" in res.stdout, res.stdout
    assert str(fake_root) in res.stdout, res.stdout
    leftover = re.findall(r"@ANTIGONA_[A-Z_]+@", res.stdout)
    assert not leftover, f"unrendered placeholder token(s) survived: {sorted(set(leftover))}\n{res.stdout}"
