from __future__ import annotations

from typing import Any, Mapping


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    """Small JSON-schema subset sufficient for tool-call boundary validation."""
    errors: list[str] = []
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)

    for key in required:
        if key not in arguments:
            errors.append(f"Missing required argument: {key}")

    if additional is False:
        unknown = set(arguments) - set(properties)
        for key in sorted(unknown):
            errors.append(f"Unknown argument: {key}")

    for key, value in arguments.items():
        rule = properties.get(key)
        if not isinstance(rule, Mapping):
            continue
        expected = rule.get("type")
        if expected in _JSON_TYPES and not isinstance(value, _JSON_TYPES[expected]):
            errors.append(f"Argument {key!r} must be {expected}")
        enum = rule.get("enum")
        if enum is not None and value not in enum:
            errors.append(f"Argument {key!r} must be one of {enum}")

    return errors
