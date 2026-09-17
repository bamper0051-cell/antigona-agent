"""Command parity — Gateway registry is the business SSOT (ADR-0017).

Every business command served by the Gateway registry (cli channel) must be
reachable from the CLI slash catalog; business commands must not be shadowed by
local UI commands with the same name (one name = one semantics). Local UI
commands are explicitly local and separate.
"""

from __future__ import annotations

from antigona.cli_ui.command_menu import LOCAL_UI_COMMANDS
from antigona.cli_ui.prompts import DEFAULT_SLASH_COMMANDS
from antigona.core.command_registry import commands_for_channel


def test_registry_commands_reachable_from_cli_catalog() -> None:
    catalog = {c.name.lstrip("/") for c in DEFAULT_SLASH_COMMANDS}
    local = {c.name.lstrip("/") for c in LOCAL_UI_COMMANDS}
    reachable = catalog | local
    missing = sorted(
        spec.name for spec in commands_for_channel("cli") if spec.name not in reachable
    )
    assert not missing, f"Gateway business commands missing from CLI catalog: {missing}"


def test_business_commands_not_shadowed_by_local_ui() -> None:
    local = {c.name.lstrip("/") for c in LOCAL_UI_COMMANDS}
    registry = {spec.name for spec in commands_for_channel("cli")}
    overlap = sorted(registry & local)
    assert not overlap, (
        f"local UI commands shadow business registry names: {overlap} "
        "(one name must not carry two semantics)"
    )


def test_local_ui_commands_are_explicitly_local() -> None:
    """Local commands are CLI-owned; the registry must not need them."""
    registry = {spec.name for spec in commands_for_channel("cli")}
    for command in LOCAL_UI_COMMANDS:
        assert command.name.lstrip("/") not in registry
