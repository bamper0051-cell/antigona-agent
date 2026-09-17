"""PR-01 contract inventory and architecture guard tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_IMPORT_ALLOWLIST = {
    "channels/telegram/context_adapter.py",
    "channels/telegram/status_renderer.py",
    "tasks/__init__.py",
    "tools/__init__.py",
    "tools/filesystem_read.py",
    "tools/filesystem_write.py",
    "tools/archive_ops.py",
    "tools/capability_registry.py",
    "tools/document_ops.py",
    "tools/registry.py",
    "tools/system_time.py",
    "tools/terminal.py",
    "tools/tts_tool.py",
}


def _load_guard():
    spec = importlib.util.spec_from_file_location("arch_guard", ROOT / "scripts" / "arch_guard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_event_bus_duplicates_are_explicitly_marked() -> None:
    contracts = (ROOT / "docs" / "architecture-contracts-inventory.md").read_text()
    expected = (
        "src/antigona/contracts.py::Tool",
        "src/antigona/tools/contracts.py::Tool",
        "src/antigona/tools/contracts.py::ToolSpec",
        "src/antigona/tools/registry.py::Tool",
        "src/antigona/events/bus.py::EventBus",
        "src/antigona/tasks/event_bus.py::EventBus",
        "canonical",
        "legacy",
        "deprecated",
    )
    for marker in expected:
        assert marker in contracts


def test_legacy_contract_imports_are_only_allowlisted_adapters() -> None:
    guard = _load_guard()
    legacy_modules = ("antigona.tools.contracts", "antigona.tasks.event_bus")
    violations: list[str] = []
    for source in (ROOT / "src" / "antigona").rglob("*.py"):
        rel = source.relative_to(ROOT / "src" / "antigona")
        if guard.is_adapter_module(source):
            continue
        text = source.read_text(encoding="utf-8")
        if any(module in text for module in legacy_modules):
            violations.append(rel.as_posix())

    assert set(violations) <= LEGACY_IMPORT_ALLOWLIST
    assert set(violations) == LEGACY_IMPORT_ALLOWLIST


def test_new_guard_patterns_find_fixture_violations() -> None:
    guard = _load_guard()
    fixtures = {
        "domain/tool.py": "from antigona.tools.contracts import Tool\n",
        "domain/events.py": "from antigona.tasks.event_bus import EventBus\n",
        "domain/provider.py": "from openai import AsyncOpenAI\n",
    }
    expected = {
        "domain/tool.py": "P7_legacy_tool_abc",
        "domain/events.py": "P8_legacy_event_bus",
        "domain/provider.py": "P9_direct_provider_sdk",
    }
    for filename, text in fixtures.items():
        hits = guard.scan_text(text, Path(filename))
        assert expected[filename] in hits


def test_new_guard_patterns_allow_adapter_modules() -> None:
    guard = _load_guard()
    text = (
        "from antigona.tools.contracts import Tool\n"
        "from antigona.tasks.event_bus import EventBus\n"
        "from anthropic import AsyncAnthropic\n"
    )
    assert guard.scan_text(text, Path("llm/adapters/anthropic.py")) == []
