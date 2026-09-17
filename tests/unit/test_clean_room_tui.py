"""Negative probes for the P2.4 TUI: clean-room provenance and thin-client limits.

Two constraints from P2_4_PLAN.md §1.4 / §5.1 are asserted straight against the
source text, because both are invisible to behavioural tests:

* the TUI is built on Textual/Rich only — no upstream TUI framework, no layout
  lifted from another agent shell;
* the TUI cannot finalize a flow — it holds no repository, no session, and does
  not so much as name the terminal state the Verifier owns.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "antigona"


def _source(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def _imported_roots(name: str) -> set[str]:
    """Top-level package names imported by a module, absolute imports only."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# Upstream agent shells and TUI toolkits we must not vendor, plus the web/native
# UI stacks that P2.4 explicitly keeps out of scope.
FORBIDDEN_MARKERS = [
    "hermes",
    "openclaw",
    "openhands",
    "klio",
    "prompt_toolkit",
    "npyscreen",
    "urwid",
    "py_cui",
    "blessed",
    "curses",
    "electron",
    "graphviz",
]

ALLOWED_TUI_IMPORTS = {"textual", "rich", "asyncio", "typing", "__future__"}
ALLOWED_CLI_IMPORTS = {
    "asyncio", "json", "typing", "collections", "httpx", "typer", "rich",
    "websockets", "os", "pathlib", "__future__", "antigona", "uuid",
    "traceback",
}


def test_tui_has_no_upstream_ui_markers() -> None:
    lowered = _source("tui.py").lower()
    for banned in FORBIDDEN_MARKERS:
        assert banned not in lowered, f"tui.py references forbidden UI source: {banned}"


def test_cli_has_no_upstream_ui_markers() -> None:
    lowered = _source("cli.py").lower()
    for banned in FORBIDDEN_MARKERS:
        assert banned not in lowered, f"cli.py references forbidden UI source: {banned}"


def test_tui_imports_only_allowed_packages() -> None:
    unexpected = _imported_roots("tui.py") - ALLOWED_TUI_IMPORTS
    assert not unexpected, f"tui.py imports unexpected packages: {sorted(unexpected)}"


def test_cli_imports_only_allowed_packages() -> None:
    unexpected = _imported_roots("cli.py") - ALLOWED_CLI_IMPORTS
    assert not unexpected, f"cli.py imports unexpected packages: {sorted(unexpected)}"


def test_tui_cannot_finalize_a_flow() -> None:
    """No state machine handle and no mention of the state only the Verifier sets."""
    source = _source("tui.py")
    assert "repository.transition" not in source
    assert "TaskRepository" not in source
    assert "DONE" not in source, "tui.py must not name the Verifier-owned terminal state"


def test_tui_does_not_touch_persistence() -> None:
    source = _source("tui.py")
    for banned in ("from .repository", "from .models", "from .database", "session_factory"):
        assert banned not in source, f"tui.py must stay a thin client, found: {banned}"


def test_tui_relative_imports_are_gateway_client_only() -> None:
    relative = {
        node.module
        for node in ast.walk(ast.parse(_source("tui.py")))
        if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module
    }
    assert relative == {"cli"}
