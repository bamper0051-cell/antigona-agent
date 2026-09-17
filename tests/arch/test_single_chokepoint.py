"""Constitution P1-003 compatibility path: one tool-execution chokepoint."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "antigona"
UNIFIED = SRC / "engine" / "unified_executor.py"
# Pre-unification orchestration internals and the registry backend remain
# explicitly fenced callers. ToolRegistry.dispatch is invoked by the unified
# layer and is the adapter that finally calls legacy Tool.execute().
DIRECT_EXECUTE_ALLOWLIST = {"orchestrator.py", "tools/registry.py"}


def _direct_tool_execute_calls(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if node.func.attr == "execute" and isinstance(owner, ast.Attribute):
            if owner.attr in {"tool", "shell_tool", "registry"}:
                hits.append(f"{filename}:{node.lineno}:{owner.attr}.execute")
        elif node.func.attr == "execute" and isinstance(owner, ast.Name):
            if owner.id in {"tool", "read_tool", "registry"}:
                hits.append(f"{filename}:{node.lineno}:{owner.id}.execute")
    return hits


def test_unified_tool_execution_layer_has_one_definition() -> None:
    definitions: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "UnifiedToolExecutionLayer":
                definitions.append(path.relative_to(SRC).as_posix())
    assert definitions == ["engine/unified_executor.py"]


def test_new_direct_tool_execution_bypasses_are_detected() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if path == UNIFIED or relative in DIRECT_EXECUTE_ALLOWLIST:
            continue
        violations.extend(_direct_tool_execute_calls(path.read_text(encoding="utf-8"), relative))
    assert violations == [], f"tool execution bypasses UnifiedToolExecutionLayer: {violations}"

    probe = "async def bypass(registry, request):\n    return await registry.execute(request)\n"
    assert _direct_tool_execute_calls(probe, "synthetic_bypass.py") == [
        "synthetic_bypass.py:2:registry.execute"
    ]


def test_named_execution_entrypoints_route_through_unified_layer() -> None:
    entrypoints = {
        "conversation/dialogue_engine.py": "UnifiedToolExecutionLayer",
        "core/brain.py": "UnifiedToolExecutionLayer",
        "cli_ui/chat.py": "UnifiedToolExecutionLayer",
    }
    for relative, symbol in entrypoints.items():
        source = (SRC / relative).read_text(encoding="utf-8")
        assert symbol in source, f"{relative} no longer routes through {symbol}"
