"""Regression tests: tui/panel CLI commands must not traceback when textual is absent.

Covers ggy.md P1 blocker: PUBLIC CLI ENTRYPOINT CRASH on ModuleNotFoundError.

Tests:
    A. `antigona tui` → exit 1, clean message, no ModuleNotFoundError traceback
    B. `antigona panel` → exit 1, clean message, no ModuleNotFoundError traceback
    C. canonical CLI imports (cli_ui.chat, cli_ui.renderer, core.gateway_client) succeed
       without textual installed
    D. no textual import occurs on normal CLI startup (import antigona.cli does not
       import textual at module level)
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from antigona.cli import app

runner = CliRunner()


def _absent_textual_sys_modules() -> dict[str, Any]:
    """Return sys.modules dict with textual and its submodules mapped to None."""
    mods = dict(sys.modules)
    for k in list(mods.keys()):
        if k == "textual" or k.startswith("textual.") or k in ("antigona.tui", "antigona.tui_console"):
            mods[k] = None
    if "textual" not in mods:
        mods["textual"] = None
    if "antigona.tui" not in mods:
        mods["antigona.tui"] = None
    if "antigona.tui_console" not in mods:
        mods["antigona.tui_console"] = None
    return mods


# ---------------------------------------------------------------------------
# A. antigona tui — must not traceback
# ---------------------------------------------------------------------------

class TestTuiGuard:
    def test_tui_exits_cleanly_without_textual(self) -> None:
        """tui exits with code 1 and emits an actionable message when textual is absent."""
        with patch.dict(sys.modules, _absent_textual_sys_modules()):
            result = runner.invoke(app, ["tui"], catch_exceptions=False)

        assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}"
        combined = (result.output or "") + (result.stderr or "")
        assert "textual" in combined.lower(), "Expected mention of 'textual' in output"
        assert "antigona chat" in combined, "Expected redirect hint 'antigona chat'"
        assert "ModuleNotFoundError" not in combined, (
            "Traceback leaked into output — guard not applied"
        )
        assert "Traceback" not in combined, "Raw traceback must not be visible"

    def test_tui_message_mentions_install_hint(self) -> None:
        """tui message includes the install extra hint."""
        with patch.dict(sys.modules, _absent_textual_sys_modules()):
            result = runner.invoke(app, ["tui"], catch_exceptions=False)

        combined = (result.output or "") + (result.stderr or "")
        assert "antigona[tui]" in combined, "Install hint 'antigona[tui]' must appear in message"


# ---------------------------------------------------------------------------
# B. antigona panel — must not traceback
# ---------------------------------------------------------------------------

class TestPanelGuard:
    def test_panel_exits_cleanly_without_textual(self) -> None:
        """panel exits with code 1 and emits an actionable message when textual is absent."""
        with patch.dict(sys.modules, _absent_textual_sys_modules()):
            result = runner.invoke(app, ["panel"], catch_exceptions=False)

        assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}"
        combined = (result.output or "") + (result.stderr or "")
        assert "textual" in combined.lower()
        assert "antigona chat" in combined
        assert "ModuleNotFoundError" not in combined
        assert "Traceback" not in combined

    def test_panel_message_mentions_install_hint(self) -> None:
        """panel message includes the install extra hint."""
        with patch.dict(sys.modules, _absent_textual_sys_modules()):
            result = runner.invoke(app, ["panel"], catch_exceptions=False)

        combined = (result.output or "") + (result.stderr or "")
        assert "antigona[tui]" in combined


# ---------------------------------------------------------------------------
# C. Canonical Living CLI imports succeed without textual
# ---------------------------------------------------------------------------

class TestCanonicalCliImports:
    def test_cli_ui_chat_importable_without_textual(self) -> None:
        """antigona.cli_ui.chat must be importable without textual."""
        with patch.dict(sys.modules, _absent_textual_sys_modules()):
            try:
                import importlib
                importlib.import_module("antigona.cli_ui.chat")
            except ModuleNotFoundError as exc:
                if "textual" in str(exc):
                    pytest.fail(
                        "antigona.cli_ui.chat imported textual — canonical CLI must not depend on textual"
                    )

    def test_gateway_client_importable_without_textual(self) -> None:
        """antigona.core.gateway_client must be importable without textual."""
        with patch.dict(sys.modules, _absent_textual_sys_modules()):
            try:
                import importlib
                importlib.import_module("antigona.core.gateway_client")
            except ModuleNotFoundError as exc:
                if "textual" in str(exc):
                    pytest.fail(
                        "antigona.core.gateway_client imported textual unexpectedly"
                    )


# ---------------------------------------------------------------------------
# D. antigona.cli module-level import does NOT import textual
# ---------------------------------------------------------------------------

class TestNoTextualOnNormalImport:
    def test_cli_module_does_not_import_textual_at_top_level(self) -> None:
        """Importing antigona.cli must not trigger a textual import at module level."""
        import importlib

        to_remove = [k for k in sys.modules if k in ("antigona.tui", "antigona.tui_console")]
        saved = {k: sys.modules.pop(k) for k in to_remove}
        try:
            textual_before = {k for k in sys.modules if k == "textual" or k.startswith("textual.")}
            importlib.reload(sys.modules["antigona.cli"])
            textual_after = {k for k in sys.modules if k == "textual" or k.startswith("textual.")}
            new_textual = textual_after - textual_before
            assert not new_textual, (
                f"Importing antigona.cli triggered textual import at module level: {new_textual}"
            )
        finally:
            sys.modules.update(saved)
