"""Fixture checks for contracts/commands.json (ADR-0017).

The Gateway registry (core/command_registry.py, served by GET /commands) is the
business SSOT. contracts/commands.json is a drift-checked projection of that
registry for the CLI: every slash command in the file must match the registry
exactly (shape and values), and system_commands must mirror the typer-level
command tree. The file is never loaded at runtime to build the CLI.
"""

from __future__ import annotations

import json
from typing import Any

from antigona.core.command_registry import commands_for_channel, find_command
from antigona.foundation import foundation_root

_CONTRACTS = foundation_root() / "contracts"


def _load(name: str) -> dict[str, Any]:
    path = _CONTRACTS / name
    assert path.exists(), f"missing contract {name}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_commands_json_is_json_schema_document() -> None:
    doc = _load("commands.json")
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(doc.keys()) >= {"system_commands", "slash_commands"}


def test_slash_commands_match_registry_exactly() -> None:
    doc = _load("commands.json")
    file_names = {cmd["name"] for cmd in doc["slash_commands"]}
    registry_names = {spec.name for spec in commands_for_channel("cli")}
    assert file_names == registry_names


def test_every_slash_command_resolves_in_registry() -> None:
    doc = _load("commands.json")
    for cmd in doc["slash_commands"]:
        assert find_command(cmd["name"]) is not None, cmd["name"]


def test_slash_descriptor_shape_matches_command_spec() -> None:
    doc = _load("commands.json")
    registry = {
        spec.name: set(spec.to_dict().keys()) for spec in commands_for_channel("cli")
    }
    for cmd in doc["slash_commands"]:
        assert set(cmd.keys()) == registry[cmd["name"]], cmd["name"]


def test_slash_command_fields_match_registry_values() -> None:
    """Drift check: fixture values must never diverge from the registry."""
    doc = _load("commands.json")
    registry = {spec.name: spec.to_dict() for spec in commands_for_channel("cli")}
    for cmd in doc["slash_commands"]:
        expected = registry[cmd["name"]]
        for key, value in expected.items():
            assert cmd[key] == value, f"{cmd['name']}.{key}"


def test_system_commands_have_required_fields() -> None:
    doc = _load("commands.json")
    for cmd in doc["system_commands"]:
        assert cmd["name"]
        assert cmd["description"]
        assert cmd["usage"]
        assert cmd["status"] in {"implemented", "planned"}
        assert isinstance(cmd["read_only"], bool)
        assert isinstance(cmd["flags"], list)


def test_no_bypass_residue_in_commands_json() -> None:
    """ADR-0017: the rejected bypass draft left no trace in the contract."""
    doc = _load("commands.json")
    names = [cmd["name"] for cmd in doc["system_commands"]]
    assert not [name for name in names if name.startswith("ops")], (
        "bypass 'ops' commands must not be advertised"
    )
