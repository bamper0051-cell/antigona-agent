"""D9 regression — no hardcoded home paths in the bubblewrap sandbox builder.

Defect D9 (canonical, reproduced while building the public tree from
``5e271c7c``): ``src/antigona/orchestration/autonomy.py`` hardcoded ``/root/...``
literals that are *functional* — the ``--ro-bind`` sources of the bubblewrap
sandbox and the credential paths.  The public-tree redaction rewrites those
literals to a different user's home (``/home/<user>/...``, rules R14-R18), the
bind source then does not exist on a normal machine and ``bwrap`` refuses to
start::

    bwrap: Can't find source path /home/<user>/.local/bin: No such file or directory

This test fails if the literals come back.  It is honest in the sense that it
does not merely grep: it drives the real command-building code path (with
``subprocess.run`` captured, exactly like the existing boundary tests) and
checks the produced ``--ro-bind``/``--bind`` sources against the filesystem.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from antigona.core import paths
from antigona.orchestration.autonomy import WorkspaceBoundary

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTONOMY_MODULE = REPO_ROOT / "src" / "antigona" / "orchestration" / "autonomy.py"

# Built without the literal appearing verbatim in this file (the portability
# guard in scripts/arch_guard.py forbids a bare ``/home/<user>/`` token in tests).
FORBIDDEN_HOME_TOKENS = ("/root/", "/home/" + "user/")

BIND_FLAGS = {"--ro-bind", "--bind"}


def _built_command(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, argv: list[str]
) -> list[str]:
    """Return the real bubblewrap argv with ``subprocess.run`` captured."""
    captured: dict[str, list[str]] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        command = args[0]
        assert isinstance(command, list)
        captured["cmd"] = [str(item) for item in command]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    WorkspaceBoundary(workspace, writable=False).run(argv, timeout=10)
    return captured["cmd"]


def _bind_sources(command: list[str]) -> list[str]:
    return [
        command[index + 1]
        for index, arg in enumerate(command)
        if arg in BIND_FLAGS and index + 1 < len(command)
    ]


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "app.py").write_text("VALUE = 1\n")
    return workspace


def _populated_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}\n")
    (home / ".codex" / "config.toml").write_text("model = 'x'\n")
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text("{}\n")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}\n")
    (home / ".claude" / "settings.json").write_text("{}\n")
    (home / ".gemini").mkdir(parents=True)
    return home


def test_autonomy_module_has_no_hardcoded_home_literals() -> None:
    """The functional home literals must not come back into the module."""
    source = AUTONOMY_MODULE.read_text(encoding="utf-8")
    found = [token for token in FORBIDDEN_HOME_TOKENS if token in source]
    assert found == [], (
        f"hardcoded home literal(s) {found} returned to {AUTONOMY_MODULE}: the "
        "sandbox bind sources must be derived from antigona.core.paths.home_dir()"
    )


def test_home_dir_helper_honours_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(paths.HOME_DIR_ENV, raising=False)
    assert paths.home_dir() == Path.home()
    override = tmp_path / "elsewhere"
    monkeypatch.setenv(paths.HOME_DIR_ENV, str(override))
    assert paths.home_dir() == override


def test_every_ro_bind_source_exists_with_empty_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a 'normal machine' (home without agent credentials) bwrap must start.

    Every ``--ro-bind`` source in the built command must exist on disk, and the
    optional credential/tool sources must simply be absent from the command.
    """
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv(paths.HOME_DIR_ENV, str(empty_home))
    workspace = _workspace(tmp_path)

    command = _built_command(monkeypatch, workspace, ["/bin/true"])

    sources = _bind_sources(command)
    missing = [source for source in sources if not Path(source).exists()]
    assert missing == [], f"bubblewrap would refuse to start, missing bind sources: {missing}"
    joined = " ".join(command)
    assert str(empty_home) in joined
    for token in FORBIDDEN_HOME_TOKENS:
        assert token not in joined, f"stale hardcoded home literal in built command: {token}"
    assert str(empty_home / ".local" / "bin") not in joined  # absent -> not bound


def test_credential_binds_follow_the_configured_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A populated home still yields the credential binds (semantics preserved)."""
    home = _populated_home(tmp_path)
    monkeypatch.setenv(paths.HOME_DIR_ENV, str(home))
    workspace = _workspace(tmp_path)

    command = _built_command(monkeypatch, workspace, ["/bin/true"])

    sources = _bind_sources(command)
    missing = [source for source in sources if not Path(source).exists()]
    assert missing == [], f"missing bind sources: {missing}"
    joined = " ".join(command)
    for expected in (
        str(home / ".local" / "bin"),
        str(home / ".codex" / "auth.json"),
        str(home / ".codex" / "config.toml"),
        str(home / ".grok" / "auth.json"),
        str(home / ".claude" / ".credentials.json"),
        str(home / ".claude" / "settings.json"),
        str(home / ".gemini"),
    ):
        assert expected in joined, f"expected credential bind for {expected}"
    assert f"--tmpfs {home}" in joined  # the live home is hidden by a tmpfs
    for token in FORBIDDEN_HOME_TOKENS:
        assert token not in joined
