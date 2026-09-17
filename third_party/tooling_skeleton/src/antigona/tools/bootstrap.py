from __future__ import annotations

from .builtins import read_file_spec, run_pytest_spec, search_files_spec, terminal_spec
from .registry import ToolRegistry


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_many([
        read_file_spec(),
        search_files_spec(),
        terminal_spec(),
        run_pytest_spec(),
    ])
    return registry
