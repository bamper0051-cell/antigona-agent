from __future__ import annotations

from prompt_toolkit.formatted_text import HTML

from antigona.cli_ui.command_menu import (
    LOCAL_UI_COMMANDS,
    build_command_toolbar,
    build_slash_catalog,
    merge_catalog,
)
from antigona.cli_ui.prompts import DEFAULT_SLASH_COMMANDS, SlashCommand
from antigona.core.command_registry import commands_for_channel


def _gateway_dto() -> list[dict[str, object]]:
    return [
        {
            "name": "tasks",
            "description": "Список задач",
            "usage": "/tasks",
            "arguments": [],
            "flags": [],
            "examples": ["/tasks"],
            "category": "tasks",
            "emoji": "📌",
            "long_description": "Список активных задач",
            "read_only": True,
            "idempotent": True,
            "destructive": False,
            "requires_approval": False,
            "risk_level": "LOW",
        },
        {
            "name": "approve",
            "description": "Одобрить операцию",
            "usage": "/approve [approval_id]",
            "arguments": ["approval_id?"],
            "flags": ["--all", "--details"],
            "examples": ["/approve", "/approve 7f3a9c"],
            "category": "approvals",
            "emoji": "✅",
            "long_description": "Одобрить операцию без ручного ID",
            "read_only": False,
            "idempotent": True,
            "destructive": False,
            "requires_approval": False,
            "risk_level": "HIGH",
        },
        {
            "name": "",
            "description": "пустое имя — пропускается",
            "usage": "/",
            "category": "",
        },
    ]


def test_build_slash_catalog_maps_dto() -> None:
    catalog = build_slash_catalog(_gateway_dto())
    names = [c.name for c in catalog]
    assert "/tasks" in names
    assert "/approve" in names
    # empty-name DTO is skipped, deduplication works
    assert len(catalog) == 2


def test_build_slash_catalog_sorted_by_category() -> None:
    catalog = build_slash_catalog(_gateway_dto())
    assert catalog[0].name == "/tasks"  # tasks category first
    assert catalog[1].name == "/approve"  # approvals second
    assert catalog[0].emoji == "📌"
    assert catalog[1].risk_level == "HIGH"
    assert catalog[1].flags == ("--all", "--details")


def test_merge_catalog_with_gateway() -> None:
    catalog = merge_catalog(_gateway_dto())
    names = [c.name for c in catalog]
    assert "/tasks" in names
    assert "/approve" in names
    assert "/clear" in names  # local UI always present
    assert "/theme" in names
    assert names.count("/clear") == 1  # dedup


def test_merge_catalog_fallback_without_gateway() -> None:
    catalog = merge_catalog(None)
    names = [c.name for c in catalog]
    assert "/help" in names
    assert "/clear" in names
    assert "/theme" in names
    assert "/memory" in names


def test_local_ui_commands_defined() -> None:
    """LOCAL_UI_COMMANDS is the client-owned local catalog (ADR-0017 norm 7).

    Canonical contract: the catalog always advertises the founding
    presentation commands /clear and /theme; it never overlaps the Gateway
    business registry (one name = one semantics — cross-checked against the
    registry in tests/architecture/test_command_parity.py); every entry is
    structurally valid (usage + category present).
    """
    local = {c.name for c in LOCAL_UI_COMMANDS}
    assert {"/clear", "/theme"} <= local
    registry = {f"/{spec.name}" for spec in commands_for_channel("cli")}
    assert local.isdisjoint(registry), (
        f"local UI commands must not shadow business registry names: "
        f"{sorted(local & registry)}"
    )
    for c in LOCAL_UI_COMMANDS:
        assert c.usage
        assert c.category


def _fake_completion_toolbar(catalog: tuple[SlashCommand, ...], selected: str) -> HTML:
    """Render the card for a given command via a direct card call (no PT app)."""
    toolbar = build_command_toolbar(catalog, fallback=lambda: HTML("✦ idle"))
    # Without an active prompt_toolkit app the toolbar falls back to status bar.
    return toolbar()


def test_build_command_toolbar_fallback_without_app() -> None:
    catalog = merge_catalog(_gateway_dto())
    toolbar = build_command_toolbar(catalog, fallback=lambda: HTML("✦ idle"))
    html = toolbar()
    assert "✦ idle" in str(html)


def test_build_slash_catalog_usage_default() -> None:
    catalog = build_slash_catalog([{"name": "health", "description": "хелс"}])
    assert catalog[0].usage == "/health"


def test_default_slash_commands_have_clear_and_theme() -> None:
    names = [c.name for c in DEFAULT_SLASH_COMMANDS]
    assert "/clear" in names
    # /theme, /alias, /sessions are implemented (2026-08-10) and advertised.
    assert "/theme" in names
    assert "/alias" in names
    assert "/sessions" in names
    assert "/uptime" in names
    assert "/export" in names
    assert "/memory" in names
    assert "/approve" in names
    assert "/deny" in names
