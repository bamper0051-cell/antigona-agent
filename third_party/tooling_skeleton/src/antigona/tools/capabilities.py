from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import ToolContext
from .registry import ToolRegistry


@dataclass(frozen=True)
class CapabilitySnapshot:
    registry_generation: int
    platform: str
    available_tools: tuple[str, ...]
    unavailable_tools: Mapping[str, str]
    schemas_for_model: tuple[Mapping[str, Any], ...]


def build_capability_snapshot(
    registry: ToolRegistry,
    context: ToolContext,
) -> CapabilitySnapshot:
    available: list[str] = []
    unavailable: dict[str, str] = {}
    schemas: list[Mapping[str, Any]] = []

    for spec in registry.snapshot():
        try:
            ok = spec.availability_check is None or spec.availability_check(context)
        except Exception as exc:
            ok = False
            unavailable[spec.name] = type(exc).__name__
        if not ok:
            unavailable.setdefault(spec.name, "availability check failed")
            continue

        available.append(spec.name)
        schemas.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.schema_for(context),
            },
        })

    return CapabilitySnapshot(
        registry_generation=registry.generation,
        platform=context.platform,
        available_tools=tuple(sorted(available)),
        unavailable_tools=unavailable,
        schemas_for_model=tuple(schemas),
    )
