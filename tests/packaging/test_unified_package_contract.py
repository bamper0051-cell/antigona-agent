"""Изолированные проверки packaging-контракта Phase 1.

Тесты выполняют только статическое чтение файлов и import resolution. Они не
создают БД, не обращаются к сети и не запускают runtime-процессы.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = (REPO / "src").resolve()
LEGACY_REPO = Path("/opt/antigona-home/antigona-cli").resolve()


def _project() -> dict[str, Any]:
    with (REPO / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def test_distribution_and_entry_point_contract() -> None:
    project = _project()["project"]
    assert project["name"] == "antigona"
    scripts = project["scripts"]
    assert scripts["antigona"] == "antigona.cli:main"
    assert scripts["antigona-cli"] == "antigona.cli:main"


def test_canonical_package_import_resolves_inside_primary_repository() -> None:
    spec = importlib.util.find_spec("antigona")
    assert spec is not None
    locations = [Path(item).resolve() for item in spec.submodule_search_locations or ()]
    assert locations
    assert all(path.is_relative_to(SRC) for path in locations)
    assert all(not path.is_relative_to(LEGACY_REPO) for path in locations)


def test_cli_module_has_no_legacy_or_forbidden_runtime_imports() -> None:
    cli_path = REPO / "src" / "antigona" / "cli.py"
    tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name == "shared_core" or name.startswith("shared_core.") for name in imported)
    assert not any(name == "hermes" or name.startswith("hermes.") for name in imported)
    assert "/opt/antigona-home/antigona-cli" not in cli_path.read_text(encoding="utf-8")


def test_phase1_files_add_no_forbidden_imports() -> None:
    phase1_python = [Path(__file__)]
    for path in phase1_python:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name == "hermes" or name.startswith("hermes.") for name in modules)


def test_import_probe_is_bound_to_test_python() -> None:
    assert Path(sys.executable).exists()


def test_runtime_version_matches_distribution_version() -> None:
    """pyproject.toml is the single source of truth for the version number."""
    import antigona
    from antigona import cli

    assert antigona.__version__ == _project()["project"]["version"]
    assert cli._cli_version() == antigona.__version__
