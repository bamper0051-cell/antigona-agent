"""Persistent slash-command aliases for the Antigona CLI.

An alias maps a short name to a command (e.g. ``s`` -> ``/status``). Typing
``/s`` or ``s`` expands to the full command before dispatch. Aliases are
stored in a small JSON under ``owner_dir()`` so they survive restarts; the
storage path is overridable via ``ANTIGONA_CLI_ALIASES_FILE`` (tests).
"""

from __future__ import annotations

import json
import os
import re

from antigona.core.paths import cli_aliases_file

_ALIAS_FILE_ENV = "ANTIGONA_CLI_ALIASES_FILE"

#: Alias names: letters/digits/underscore/hyphen, no spaces, no leading "/".
_ALIAS_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def aliases_file() -> str:
    """Governed runtime path for the persisted aliases (never the code root)."""
    return str(cli_aliases_file())


def load_aliases() -> dict[str, str]:
    try:
        with open(aliases_file(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def save_aliases(aliases: dict[str, str]) -> None:
    path = aliases_file()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(aliases, f, ensure_ascii=False, indent=2)


def valid_name(name: str) -> bool:
    """Whether *name* is acceptable as an alias identifier (no leading slash)."""
    return bool(_ALIAS_NAME_RE.match(name))


def set_alias(name: str, command: str) -> None:
    if not valid_name(name):
        raise ValueError(f"Недопустимое имя алиаса: {name!r}")
    if not command.strip():
        raise ValueError("Команда алиаса не может быть пустой.")
    aliases = load_aliases()
    aliases[name] = command
    save_aliases(aliases)


def delete_alias(name: str) -> bool:
    aliases = load_aliases()
    if name not in aliases:
        return False
    del aliases[name]
    save_aliases(aliases)
    return True


def expand(text: str) -> str:
    """Expand a leading alias token in *text* (e.g. ``/s`` -> ``/status``).

    Only the first whitespace-separated token is considered; both ``/name``
    and bare ``name`` forms match. Returns the input unchanged when no alias
    matches.
    """
    stripped = text.lstrip()
    if not stripped:
        return text
    first, _, rest = stripped.partition(" ")
    key = first.lstrip("/")
    aliases = load_aliases()
    if key in aliases:
        # Preserve the original leading whitespace and the rest of the line.
        indent = text[: len(text) - len(stripped)]
        suffix = (" " + rest) if rest else ""
        return indent + aliases[key] + suffix
    return text


__all__ = [
    "aliases_file", "load_aliases", "save_aliases", "valid_name",
    "set_alias", "delete_alias", "expand",
]
