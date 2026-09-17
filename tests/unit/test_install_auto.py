"""Unit tests for the /install-auto (project manifest auto-install) capability.
"""
from pathlib import Path

import pytest

from antigona.router.intent_router import IntentRouter


@pytest.fixture()
def router():
    return IntentRouter()


@pytest.mark.parametrize("cmd,intent", [
    ("/install-auto proj", "command.install_auto"),
    ("/installauto proj", "command.install_auto"),
    ("/autoload proj", "command.install_auto"),
])
def test_install_auto_routes(router, cmd, intent):
    assert router.route(cmd, context={}).intent == intent


def test_detect_install_plan_pip(tmp_path: Path):
    from antigona.core.brain import AntigonaBrain
    (tmp_path / "requirements.txt").write_text("six\n")
    plans = AntigonaBrain._detect_install_plan(str(tmp_path), "proj")
    labels = [p[0] for p in plans]
    assert "pip" in labels
    cmd = next(p[1] for p in plans if p[0] == "pip")
    # relative path, no leading slash -> passes is_sensitive_command
    assert cmd[0] == "pip"
    assert not cmd[-1].startswith("/")


def test_detect_install_plan_multiple(tmp_path: Path):
    from antigona.core.brain import AntigonaBrain
    (tmp_path / "requirements.txt").write_text("x\n")
    (tmp_path / "package.json").write_text("{}")
    plans = AntigonaBrain._detect_install_plan(str(tmp_path), "proj")
    assert sorted(p[0] for p in plans) == ["npm", "pip"]


def test_install_auto_command_policy_safe(tmp_path: Path):
    from antigona.core.brain import AntigonaBrain
    from antigona.result_safety import is_sensitive_command
    (tmp_path / "requirements.txt").write_text("six\n")
    plans = AntigonaBrain._detect_install_plan(str(tmp_path), "proj")
    for _, cmd in plans:
        assert is_sensitive_command(cmd) is False, cmd
