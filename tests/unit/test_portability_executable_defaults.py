"""R1-PORTABILITY-01 (T0021 R1-B08) — executable storage defaults must resolve
through the canonical ``antigona.core.paths`` API, never a hardcoded
``/opt/antigona-home/.antigona`` path (which pollutes Windows with a ``C:\\root`` tree and
``PermissionError``-s on non-root Linux).

Regression targets:
  - ``antigona.skills.plugin_loader.SkillRegistry.__init__`` default
    ``Path("/opt/antigona-home/.antigona/skills")``
  - ``antigona.tools.registry._handle_kanban`` default board
    ``"/opt/antigona-home/.antigona/workspace/.kanban"``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from antigona.core import paths


@pytest.fixture(autouse=True)
def _isolate_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path / "proj"))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr(paths, "owner_dir", lambda: tmp_path / "owner")
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)


def _norm(p: object) -> str:
    return str(p).replace("\\", "/")


def test_skill_registry_default_root_is_canonical() -> None:
    from antigona.skills.plugin_loader import SkillRegistry

    reg = SkillRegistry()
    assert _norm(reg._skills_root) == _norm(paths.skills_dir())
    assert paths.owner_dir() in (reg._skills_root.parents or ()) or _norm(reg._skills_root).startswith(_norm(paths.owner_dir()))
    assert reg._skills_root.exists()


def test_skill_registry_explicit_root_still_honoured(tmp_path: Path) -> None:
    from antigona.skills.plugin_loader import SkillRegistry

    explicit = tmp_path / "custom-skills"
    reg = SkillRegistry(skills_root=str(explicit))
    assert _norm(reg._skills_root) == _norm(explicit)


def test_kanban_dir_helper_is_workspace_relative() -> None:
    assert _norm(paths.kanban_dir()) == _norm(paths.workspace_dir() / ".kanban")
    assert _norm(paths.kanban_dir()).startswith(_norm(paths.workspace_dir()))


@pytest.mark.anyio
async def test_handle_kanban_default_board_is_canonical() -> None:
    from antigona.tools.registry import _handle_kanban

    out = json.loads(await _handle_kanban(action="create", title="portability"))
    assert out.get("success") is True
    board = paths.kanban_dir()
    # the card landed under the canonical workspace board, not /opt/antigona-home
    assert board.exists() and (board / "todo").is_dir()
    assert _norm(board).startswith(_norm(paths.workspace_dir()))


@pytest.mark.anyio
async def test_handle_kanban_explicit_board_still_honoured(tmp_path: Path) -> None:
    from antigona.tools.registry import _handle_kanban

    explicit = tmp_path / "explicit-board"
    out = json.loads(
        await _handle_kanban(action="create", title="x", board=str(explicit))
    )
    assert out.get("success") is True
    assert (explicit / "todo").is_dir()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
