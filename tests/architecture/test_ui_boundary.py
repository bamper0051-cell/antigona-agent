"""P10 — UI client boundary (ADR-0017, docs/architecture/cli-boundary.md).

UI packages (antigona.cli, antigona.cli_ui, antigona.tui, antigona.tui_console)
are thin presenters over the Gateway. They must not import the durable layer,
ORM models, worker/verifier internals, providers or Redis — any such import
creates a second execution path that bypasses owner isolation, policy,
approvals, the journal and Verifier-only completion.

Scope note: channels/telegram is deliberately NOT covered here — the bot is a
server-side delivery adapter that legitimately owns Telegram operation state
(see its imports of antigona.database / antigona.durable); its boundary is a
separate concern.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "antigona"

# Modules a UI package must never import (P10).
FORBIDDEN_MODULES = (
    "antigona.database",
    "antigona.repository",
    "antigona.models",
    "antigona.storage",
    "antigona.durable",
    "antigona.worker",
    "antigona.verifier",
    "antigona.providers",
    "antigona.agent",
    "redis",
)

UI_PACKAGES = ("cli.py", "tui.py", "tui_console.py", "cli_ui")


def _ui_files() -> list[Path]:
    files: list[Path] = []
    for name in UI_PACKAGES:
        path = SRC / name
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in str(p)))
    return files


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == mod or alias.name.startswith(f"{mod}.")
                    for mod in FORBIDDEN_MODULES
                ):
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(
                module == mod or module.startswith(f"{mod}.") for mod in FORBIDDEN_MODULES
            ):
                hits.append(f"from {module} import ...")
    return hits


def test_ui_packages_have_no_durable_imports() -> None:
    violations: dict[str, list[str]] = {}
    for path in _ui_files():
        hits = _forbidden_imports(path)
        if hits:
            violations[str(path.relative_to(SRC))] = hits
    assert not violations, f"P10 violations: {violations}"


def test_ui_boundary_documents_scope() -> None:
    """The boundary doc exists and names the same forbidden surface."""
    doc = (ROOT / "docs" / "architecture" / "cli-boundary.md").read_text(
        encoding="utf-8"
    )
    # Short names only: the doc names the forbidden surface in prose
    # (e.g. "database, repository, ORM models, storage, durable, worker,
    # verifier, providers, redis") without the antigona.* prefix.
    short_names = tuple(mod.removeprefix("antigona.") for mod in FORBIDDEN_MODULES)
    for name in short_names:
        assert name in doc, f"cli-boundary.md must mention {name}"
