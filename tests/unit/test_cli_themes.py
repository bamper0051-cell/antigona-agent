"""Tests for the CLI theme system (/theme + themes registry)."""

from __future__ import annotations

import json
import os
from unittest.mock import Mock

import pytest

from antigona.cli_ui import themes
from antigona.cli_ui.layout import AntigonaLayout
from antigona.cli_ui.models import ChatUIState
from antigona.cli_ui.renderer import CliRenderer


@pytest.fixture(autouse=True)
def _isolate_theme_file(tmp_path, monkeypatch):
    # isolate persistence to a temp file and reset process cache each test
    monkeypatch.setenv("ANTIGONA_CLI_THEME_FILE", str(tmp_path / "theme.json"))
    monkeypatch.setattr(themes, "_active", None)
    yield


def test_registry_has_many_distinct_themes() -> None:
    assert len(themes.THEMES) >= 8
    names = set(themes.THEMES)
    assert {"aurora", "neon", "hacker", "ocean", "forest", "sunset", "dark", "mono"} <= names


def test_aurora_default_matches_legacy_palette() -> None:
    t = themes.get_active()
    assert t.accent == "#56E2A0"
    assert t.violet == "#7C3AED"
    assert t.header_bg == "#7C3AED"


def test_set_active_persists(tmp_path) -> None:
    themes.set_active("neon")
    path = os.path.join(str(tmp_path), "theme.json")
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["theme"] == "neon"


def test_set_active_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        themes.set_active("nope")


def test_layout_apply_theme_switches_style() -> None:
    async def on_input(text: str) -> None:
        pass

    state = ChatUIState(messages=[], current_status="idle", events=[],
                        connection="connected", gateway_url="http://x", session_id="s")
    layout = AntigonaLayout(state=state, renderer=Mock(spec=CliRenderer), on_input=on_input)
    before = layout.theme.name
    assert layout.app_style is not None

    ok = layout.apply_theme("hacker")
    assert ok is True
    assert layout.theme.name == "hacker"
    assert layout.theme != before

    assert layout.apply_theme("does_not_exist") is False
    assert layout.theme.name == "hacker"  # unchanged on failure


def test_layout_apply_theme_repaints() -> None:
    async def on_input(text: str) -> None:
        pass

    state = ChatUIState(messages=[], current_status="idle", events=[],
                        connection="connected", gateway_url="http://x", session_id="s")
    layout = AntigonaLayout(state=state, renderer=Mock(spec=CliRenderer), on_input=on_input)
    layout.request_repaint = Mock()
    layout.application = Mock()
    layout.apply_theme("ocean")
    layout.request_repaint.assert_called_once()


# ── custom theme mode ─────────────────────────────────────────────────────

def test_registry_has_many_themes_now() -> None:
    assert len(themes.THEMES) >= 16
    assert "nord" in themes.THEMES and "gruvbox" in themes.THEMES


def test_set_custom_overrides_and_defaults() -> None:
    t = themes.set_custom({"accent": "#FF0000", "header_bg": "#00FF00"})
    assert t.name == "custom"
    assert t.accent == "#ff0000"
    assert t.header_bg == "#00ff00"
    # unspecified slots fall back to aurora
    assert t.bg == themes.AURORA.bg
    assert t.muted_lavender == themes.AURORA.muted_lavender


def test_parse_custom_args_positional() -> None:
    slots = themes.parse_custom_args(("#00FFCC", "#FF2BD6"))
    assert slots == {"accent": "#00ffcc", "violet": "#ff2bd6"}
    with pytest.raises(ValueError):
        themes.parse_custom_args(("not-a-color",))
    with pytest.raises(ValueError):
        themes.parse_custom_args(())


def test_layout_apply_custom_theme() -> None:
    async def on_input(text: str) -> None:
        pass

    state = ChatUIState(messages=[], current_status="idle", events=[],
                        connection="connected", gateway_url="http://x", session_id="s")
    layout = AntigonaLayout(state=state, renderer=Mock(spec=CliRenderer), on_input=on_input)
    ok = layout.apply_custom_theme({"accent": "#112233"})
    assert ok is True
    assert layout.theme.name == "custom"
    assert layout.theme.accent == "#112233"


# ── save custom as named theme + Tab completion ───────────────────────────

def test_save_user_theme_and_restore(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTIGONA_CLI_USER_THEMES_FILE", str(tmp_path / "user_themes.json"))
    saved = themes.save_user_theme("mydark", {"accent": "#123456", "header_bg": "#0000FF"})
    assert saved.name == "mydark"
    assert saved.accent == "#123456"

    # restore by name from a fresh process cache
    monkeypatch.setattr(themes, "_active", None)
    restored = themes.set_active("mydark")
    assert restored.name == "mydark"
    assert restored.accent == "#123456"


def test_save_user_theme_rejects_builtin_collision(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTIGONA_CLI_USER_THEMES_FILE", str(tmp_path / "user_themes.json"))
    with pytest.raises(ValueError):
        themes.save_user_theme("neon", {"accent": "#fff"})


def test_list_themes_includes_user_theme(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTIGONA_CLI_USER_THEMES_FILE", str(tmp_path / "user_themes.json"))
    themes.save_user_theme("my-skin", {"accent": "#abcdef"})
    names = [t.name for t in themes.list_themes()]
    assert "my-skin" in names
    assert "my-skin" in themes.theme_names()


def test_completer_suggests_theme_names() -> None:
    from prompt_toolkit.document import Document

    from antigona.cli_ui.prompts import SlashCommandCompleter

    c = SlashCommandCompleter()
    hits = [x.text for x in c.get_completions(Document(text="/theme n"))]
    assert "neon" in hits and "nord" in hits
    hits_all = list(c.get_completions(Document(text="/theme ")))
    assert len(hits_all) >= 17
