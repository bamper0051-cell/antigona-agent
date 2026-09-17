"""Theme registry for the Antigona CLI full-screen layout.

Every named theme is a full colour palette for the ``antigona chat`` terminal
surface (header, input, menu, scrollbar, status/role accents). ``/theme``
switches the active theme and persists it; the layout reads ``get_active()``
at construction and re-reads it on switch, so the design changes live.

The default ``aurora`` theme reproduces the historical AURORA palette exactly
so default behaviour (and the colour assertions in the layout tests) is
unchanged. Each additional theme is a distinct terminal "skin".

Thread-safety / persistence: the active theme is a process-global read by the
single CLI process. ``set_active`` writes a tiny JSON under the governed
runtime root (``core.paths``) so
the choice survives restarts.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.styles import Style

from antigona.core.paths import cli_theme_file, cli_user_themes_file


@dataclass(frozen=True)
class Theme:
    """A full terminal colour palette for the full-screen layout."""

    name: str
    label: str
    accent: str
    violet: str
    magenta: str
    amber: str
    muted_grey: str
    muted_lavender: str
    blue: str
    red: str
    bg: str
    fg: str
    header_bg: str
    menu_bg: str
    menu_fg: str


# ── Palettes ────────────────────────────────────────────────────────────────
#: Historical default (identical to cli_ui/panel AURORA + layout defaults).
AURORA = Theme(
    name="aurora", label="Aurora (по умолчанию)",
    accent="#56E2A0", violet="#7C3AED", magenta="#E83EDC", amber="#FFC857",
    muted_grey="#5A5878", muted_lavender="#7C7A96", blue="#6EA0FF", red="#FF6E6E",
    bg="#241B36", fg="#F5F3FF", header_bg="#7C3AED", menu_bg="#241B36", menu_fg="#F5F3FF",
)

#: Bright cyan/magenta synthwave.
NEON = Theme(
    name="neon", label="Neon (кислотный)",
    accent="#00FFCC", violet="#FF2BD6", magenta="#00E5FF", amber="#FFE600",
    muted_grey="#2A2A4A", muted_lavender="#8A8AA0", blue="#00C8FF", red="#FF3B6B",
    bg="#0D0221", fg="#EAF6FF", header_bg="#1A1040", menu_bg="#0D0221", menu_fg="#EAF6FF",
)

#: Near-monochrome, minimal colour noise.
MINIMAL = Theme(
    name="minimal", label="Minimal (монохром)",
    accent="#CFD8DC", violet="#90A4AE", magenta="#B0BEC5", amber="#EEEEEE",
    muted_grey="#616161", muted_lavender="#9E9E9E", blue="#B0BEC5", red="#EF9A9A",
    bg="#111111", fg="#ECEFF1", header_bg="#263238", menu_bg="#111111", menu_fg="#ECEFF1",
)

#: Deep blue/indigo night.
DARK = Theme(
    name="dark", label="Dark (синий индиго)",
    accent="#64B5F6", violet="#5C6BC0", magenta="#9575CD", amber="#FFD54F",
    muted_grey="#3A4054", muted_lavender="#8A8FA8", blue="#42A5F5", red="#EF5350",
    bg="#0B1020", fg="#E8EAF6", header_bg="#283593", menu_bg="#0B1020", menu_fg="#E8EAF6",
)

#: Cool ocean teals.
OCEAN = Theme(
    name="ocean", label="Ocean (бирюза)",
    accent="#4DD0E1", violet="#26A69A", magenta="#80CBC4", amber="#FFD54F",
    muted_grey="#2C4A4A", muted_lavender="#80A8A8", blue="#29B6F6", red="#EF5350",
    bg="#04242B", fg="#E0F7FA", header_bg="#00695C", menu_bg="#04242B", menu_fg="#E0F7FA",
)

#: Green/forest calm.
FOREST = Theme(
    name="forest", label="Forest (лес)",
    accent="#81C784", violet="#66BB6A", magenta="#A5D6A7", amber="#FFEE58",
    muted_grey="#2E4A32", muted_lavender="#81A08A", blue="#4FC3F7", red="#E57373",
    bg="#0D1F10", fg="#E8F5E9", header_bg="#2E7D32", menu_bg="#0D1F10", menu_fg="#E8F5E9",
)

#: Warm orange/pink dusk.
SUNSET = Theme(
    name="sunset", label="Sunset (закат)",
    accent="#FFB74D", violet="#FF7043", magenta="#F06292", amber="#FFD54F",
    muted_grey="#5A3E3E", muted_lavender="#A88A8A", blue="#64B5F6", red="#E53935",
    bg="#2A1210", fg="#FFF3E0", header_bg="#D84315", menu_bg="#2A1210", menu_fg="#FFF3E0",
)

#: Grayscale with a single green accent.
MONO = Theme(
    name="mono", label="Mono (графит)",
    accent="#9CCC65", violet="#B0BEC5", magenta="#CFD8DC", amber="#F5F5F5",
    muted_grey="#424242", muted_lavender="#9E9E9E", blue="#B0BEC5", red="#E0A0A0",
    bg="#1A1A1A", fg="#EEEEEE", header_bg="#37474F", menu_bg="#1A1A1A", menu_fg="#EEEEEE",
)

#: Terminal green-on-black hacker.
HACKER = Theme(
    name="hacker", label="Hacker (зелёный)",
    accent="#33FF66", violet="#00CC44", magenta="#88FF88", amber="#FFFF66",
    muted_grey="#1E3A24", muted_lavender="#5E8C6A", blue="#22FFAA", red="#FF5555",
    bg="#000000", fg="#A6FFC0", header_bg="#003311", menu_bg="#000000", menu_fg="#A6FFC0",
)

#: Soft pastel, easy on the eyes.
PASTEL = Theme(
    name="pastel", label="Pastel (пастель)",
    accent="#B9FBC0", violet="#C3AED6", magenta="#F5C6EC", amber="#FFF3B0",
    muted_grey="#8A8A9A", muted_lavender="#B0A8C0", blue="#A8D8EA", red="#F4A9A9",
    bg="#2A2A40", fg="#FDF6E3", header_bg="#6A5ACD", menu_bg="#2A2A40", menu_fg="#FDF6E3",
)

#: Cobalt/gold electric.
CYBER = Theme(
    name="cyber", label="Cyber (кобальт/золото)",
    accent="#00E5FF", violet="#1A237E", magenta="#FFB300", amber="#FFC107",
    muted_grey="#2A2E45", muted_lavender="#7A80A0", blue="#00B0FF", red="#FF5252",
    bg="#0A0E27", fg="#E0E7FF", header_bg="#0D1B7A", menu_bg="#0A0E27", menu_fg="#E0E7FF",
)


#: Cool nordic blue-grey (VS Code Nord-ish).
NORD = Theme(
    name="nord", label="Nord (сдержанный)",
    accent="#88C0D0", violet="#81A1C1", magenta="#B48EAD", amber="#EBCB8B",
    muted_grey="#3B4252", muted_lavender="#4C566A", blue="#5E81AC", red="#BF616A",
    bg="#2E3440", fg="#D8DEE9", header_bg="#4C566A", menu_bg="#2E3440", menu_fg="#D8DEE9",
)

#: Warm retro gruvbox.
GRUVBOX = Theme(
    name="gruvbox", label="Gruvbox (тёплый)",
    accent="#B8BB26", violet="#D79921", magenta="#D3869B", amber="#FABD2F",
    muted_grey="#504945", muted_lavender="#7C6F64", blue="#83A598", red="#FB4934",
    bg="#282828", fg="#EBDBB2", header_bg="#665C54", menu_bg="#282828", menu_fg="#EBDBB2",
)

#: Bright candy pink/teal.
CANDY = Theme(
    name="candy", label="Candy (розовый)",
    accent="#FF9FEB", violet="#FF5E9C", magenta="#FFB6FF", amber="#FFE066",
    muted_grey="#5A3A5A", muted_lavender="#B08AB0", blue="#5EF0E0", red="#FF4D6D",
    bg="#2B1130", fg="#FFF0F6", header_bg="#B03078", menu_bg="#2B1130", menu_fg="#FFF0F6",
)

#: Icy pale blues.
ICE = Theme(
    name="ice", label="Ice (ледяной)",
    accent="#A5E8FF", violet="#B3D7FF", magenta="#C7E8FF", amber="#FFF3C4",
    muted_grey="#3A5060", muted_lavender="#7A9CB0", blue="#8FD3FF", red="#FFB3B3",
    bg="#0E2230", fg="#EAF7FF", header_bg="#1B3A4E", menu_bg="#0E2230", menu_fg="#EAF7FF",
)

#: Volcanic orange/red.
LAVA = Theme(
    name="lava", label="Lava (лава)",
    accent="#FFB347", violet="#FF6B4A", magenta="#FF8A5C", amber="#FFD27D",
    muted_grey="#4A2E28", muted_lavender="#A07068", blue="#6EA8FF", red="#FF4500",
    bg="#1C0F0D", fg="#FFF0E8", header_bg="#8B1A0F", menu_bg="#1C0F0D", menu_fg="#FFF0E8",
)

#: Deep midnight purple/blue.
NIGHTOWL = Theme(
    name="nightowl", label="Night Owl (ночная сова)",
    accent="#C792EA", violet="#82AAFF", magenta="#FF8FB1", amber="#FFD88F",
    muted_grey="#3A3350", muted_lavender="#7A6F9A", blue="#7ECBFF", red="#FF6B6B",
    bg="#0E0B1E", fg="#D6DEEB", header_bg="#2A2160", menu_bg="#0E0B1E", menu_fg="#D6DEEB",
)


#: Registration order (stable listing/iteration).
_REGISTRATION: tuple[Theme, ...] = (
    AURORA, NEON, MINIMAL, DARK, OCEAN, FOREST, SUNSET, MONO, HACKER, PASTEL,
    CYBER, NORD, GRUVBOX, CANDY, ICE, LAVA, NIGHTOWL,
)

THEMES: dict[str, Theme] = {t.name: t for t in _REGISTRATION}

_THEME_FILE_ENV = "ANTIGONA_CLI_THEME_FILE"

#: Positional slot order for ``/theme custom <hex...>`` (visible surfaces first).
CUSTOM_SLOT_ORDER: tuple[str, ...] = (
    "accent", "violet", "magenta", "amber", "blue", "red",
    "bg", "fg", "header_bg", "menu_bg", "menu_fg",
)

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _theme_file() -> str:
    """Governed runtime JSON path for the persisted active theme.

    Resolves through ``core.paths`` (``ANTIGONA_CLI_THEME_FILE`` >
    ``ANTIGONA_STATE_ROOT`` > fail-closed in immutable mode > dev default), so a
    running CLI never rewrites the read-only code root.
    """
    return str(cli_theme_file())


def _hex_valid(value: str) -> bool:
    """Whether *value* is a valid #RGB / #RRGGBB hex colour."""
    return bool(_HEX_RE.match(value))


#: Persisted user-defined themes (name -> slot overrides). Overridable in tests.
_USER_THEMES_FILE_ENV = "ANTIGONA_CLI_USER_THEMES_FILE"


def _user_themes_file() -> str:
    """Governed runtime path for the persisted user-defined themes."""
    return str(cli_user_themes_file())


def load_user_themes() -> dict[str, dict[str, str]]:
    """Return saved user themes as ``{name: slot_overrides}``."""
    try:
        with open(_user_themes_file(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out: dict[str, dict[str, str]] = {}
            for name, slots in data.items():
                if isinstance(slots, dict):
                    out[str(name)] = {str(k): str(v) for k, v in slots.items()}
            return out
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _save_user_themes(user: dict[str, dict[str, str]]) -> None:
    try:
        path = _user_themes_file()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(user, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _lookup_theme(name: str) -> Theme | None:
    """Built-in theme, then a saved user theme; None when unknown."""
    if name in THEMES:
        return THEMES[name]
    user = load_user_themes()
    if name in user:
        return _build_custom(user[name], name=name, label=f"Custom ({name})")
    return None


def save_user_theme(name: str, slots: dict[str, str]) -> Theme:
    """Save the current colours as a named user theme and activate it.

    *slots* maps CUSTOM_SLOT_ORDER slots to hex values (invalid ones fall
    back to aurora). Raises ValueError for a bad name or a name that collides
    with a built-in theme.
    """
    if not name or not re.match(r"^[A-Za-z0-9_-]{1,24}$", name):
        raise ValueError(f"Недопустимое имя темы: {name!r}")
    if name in THEMES:
        raise ValueError(f"Имя '{name}' занято встроенной темой.")
    theme = _build_custom(slots, name=name, label=f"Custom ({name})")
    user = load_user_themes()
    valid = {k: v.lower() for k, v in slots.items() if _hex_valid(v)}
    user[name] = valid
    _save_user_themes(user)
    global _active
    _active = theme
    _persist({"theme": name})
    return theme


def _build_custom(slots: dict[str, str], name: str = "custom", label: str = "Custom (свой)") -> Theme:
    """Build a theme from aurora defaults overridden by valid slot values."""
    base = AURORA
    merged = {
        "name": name, "label": label,
        "muted_grey": base.muted_grey, "muted_lavender": base.muted_lavender,
    }
    for slot in CUSTOM_SLOT_ORDER:
        val = slots.get(slot, "")
        merged[slot] = val.lower() if _hex_valid(val) else getattr(base, slot)
    return Theme(**merged)


#: Process-global active theme (aurora unless overridden/persisted).
_active: Theme = AURORA


def _persist(payload: dict[str, Any]) -> None:
    """Best-effort persist of the active theme (never raises)."""
    try:
        path = _theme_file()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass


def _load_persisted() -> Theme:
    try:
        if os.path.isfile(_theme_file()):
            with open(_theme_file(), encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("theme", "")
            if name == "custom":
                return _build_custom(data.get("custom") or {})
            theme = _lookup_theme(name)
            if theme is not None:
                return theme
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return AURORA


def get_active() -> Theme:
    """Return the active theme (process cache, falls back to persisted)."""
    global _active
    if _active is None:
        _active = _load_persisted()
    return _active


def set_active(name: str) -> Theme:
    """Activate a named theme (built-in or user-saved), persist it, return it.

    Raises KeyError when the name is unknown.
    """
    theme = _lookup_theme(name)
    if theme is None:
        raise KeyError(name)
    global _active
    _active = theme
    _persist({"theme": theme.name})
    return theme


def set_custom(slots: dict[str, str]) -> Theme:
    """Build, activate and persist a custom theme from validated slot overrides."""
    theme = _build_custom(slots)
    global _active
    _active = theme
    valid = {k: v.lower() for k, v in slots.items() if _hex_valid(v)}
    _persist({"theme": "custom", "custom": valid})
    return theme


def parse_custom_args(args: tuple[str, ...]) -> dict[str, str]:
    """Map positional hex args to custom slots. Raises ValueError on bad input."""
    slots: dict[str, str] = {}
    for i, arg in enumerate(args):
        if i >= len(CUSTOM_SLOT_ORDER):
            break
        if not _hex_valid(arg):
            raise ValueError(f"Недопустимый цвет: {arg!r} (нужен #RGB или #RRGGBB)")
        slots[CUSTOM_SLOT_ORDER[i]] = arg.lower()
    if not slots:
        raise ValueError("Укажите хотя бы один цвет: /theme custom #RRGGBB [...]")
    return slots


def list_themes() -> list[Theme]:
    """All built-in themes in registration order, then saved user themes."""
    themes = list(_REGISTRATION)
    for name, slots in sorted(load_user_themes().items()):
        themes.append(_build_custom(slots, name=name, label=f"Custom ({name})"))
    return themes


def theme_names() -> list[str]:
    """All selectable theme names (built-in then user-saved)."""
    return [t.name for t in list_themes()]


# ── Derived colour maps (so a theme needs only the core slots) ────────────

def status_color(theme: Theme, status: str) -> str:
    """Accent for a task status, derived from the theme's core slots."""
    if status in ("done", "verified", "idle"):
        return theme.accent
    if status in ("error", "failed", "timeout", "cancelled", "disconnected"):
        return theme.red
    if status in ("waiting_approval", "reconnecting"):
        return theme.amber
    if status in ("planning",):
        return theme.violet
    if status in ("tool_executing", "sending", "observing"):
        return theme.blue
    return theme.muted_lavender


def role_color(theme: Theme, role: str) -> str:
    """Accent for a message role in the scrollback."""
    return {
        "user": theme.blue,
        "assistant": theme.violet,
        "system": theme.muted_lavender,
        "tool": theme.accent,
        "error": theme.red,
        "warning": theme.amber,
        "info": theme.magenta,
    }.get(role, theme.muted_lavender)


def connection_color(theme: Theme, state: str) -> str:
    """Accent for the connection indicator."""
    return {
        "connected": theme.accent,
        "reconnecting": theme.amber,
        "disconnected": theme.red,
    }.get(state, theme.muted_lavender)


def build_style(theme: Theme) -> Style:
    """prompt_toolkit Style for the full-screen layout under *theme*."""
    return Style.from_dict(
        {
            "menu": f"bg:{theme.menu_bg} fg:{theme.menu_fg}",
            "scrollbar.background": f"bg:{theme.muted_grey}",
            "scrollbar.button": f"bg:{theme.violet}",
        }
    )


__all__ = [
    "AURORA", "NEON", "MINIMAL", "DARK", "OCEAN", "FOREST", "SUNSET", "MONO",
    "HACKER", "PASTEL", "CYBER", "NORD", "GRUVBOX", "CANDY", "ICE", "LAVA", "NIGHTOWL",
    "Theme", "THEMES", "CUSTOM_SLOT_ORDER", "get_active", "set_active", "set_custom",
    "parse_custom_args", "list_themes", "theme_names", "save_user_theme",
    "load_user_themes", "status_color", "role_color", "connection_color", "build_style",
]
