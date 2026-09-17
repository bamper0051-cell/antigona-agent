"""Personality — система персон агента.

Позволяет загружать и переключать системные промпты из файлов
в .antigona/personalities/, а также читать SOUL.md и AGENTS.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

from antigona.core import paths

logger = logging.getLogger(__name__)

# ─── Default paths ────────────────────────────────────────────────────────────

_ANTIGONA_DIR = paths.project_local_dir()
_PERSONALITIES_DIR = _ANTIGONA_DIR / "personalities"
_SOUL_FILE = _ANTIGONA_DIR / "SOUL.md"
_AGENTS_FILE = _ANTIGONA_DIR / "AGENTS.md"

# ─── Built-in personalities ───────────────────────────────────────────────────

_PERSONALITY_NAMES: dict[str, str] = {
    "default": "Стандартная персона — дружелюбный помощник",
    "concise": "Краткая персона — только суть",
    "creative": "Творческая персона — креативные решения",
    "teacher": "Обучающая персона — наставник",
    "technical": "Техническая персона — точные спецификации",
}

# ─── Personality class ────────────────────────────────────────────────────────


class PersonalityNotFoundError(Exception):
    """Raised when a personality file is not found."""


class PersonalityManager:
    """Manages agent personalities and context files.

    Provides methods to:
    - List available personalities
    - Load a personality (from file or built-in)
    - Read SOUL.md and AGENTS.md context files
    - Build a combined system prompt
    """

    def __init__(
        self,
        antigona_dir: str | Path | None = None,
    ) -> None:
        self._antigona_dir = Path(antigona_dir) if antigona_dir else _ANTIGONA_DIR
        self._personalities_dir = self._antigona_dir / "personalities"
        self._current_personality: str = "default"
        self._personalities_dir.mkdir(parents=True, exist_ok=True)

    # ── List personalities ─────────────────────────────────────────────────

    def list_personalities(self) -> dict[str, str]:
        """Return dict of personality_name -> description.

        Discovers .md files in the personalities directory and merges with built-in names.
        """
        result: dict[str, str] = {}
        # Built-in descriptions
        result.update(_PERSONALITY_NAMES)

        # Discover custom files
        if self._personalities_dir.exists():
            for f in self._personalities_dir.glob("*.md"):
                name = f.stem
                if name not in result:
                    result[name] = f"Кастомная персона: {name}"

        return result

    # ── Load personality ───────────────────────────────────────────────────

    def load_personality(self, name: str) -> str:
        """Load the system prompt for a given personality name.

        Falls back to 'default' if the requested personality is not found.

        Returns:
            The personality text (system prompt content).
        """
        name = name.lower().strip()
        personality_file = self._personalities_dir / f"{name}.md"

        if personality_file.exists():
            text = personality_file.read_text(encoding="utf-8").strip()
            self._current_personality = name
            return text

        # Try built-in fallback
        default_file = self._personalities_dir / "default.md"
        if default_file.exists():
            self._current_personality = "default"
            return default_file.read_text(encoding="utf-8").strip()

        raise PersonalityNotFoundError(
            f"Personality '{name}' not found and no default fallback available"
        )

    @property
    def current_personality(self) -> str:
        """Get the currently active personality name."""
        return self._current_personality

    # ── Context files ──────────────────────────────────────────────────────

    def read_soul(self) -> str:
        """Read the SOUL.md file.

        Returns:
            Contents of SOUL.md, or empty string if not found.
        """
        soul_file = self._antigona_dir / "SOUL.md"
        if soul_file.exists():
            return soul_file.read_text(encoding="utf-8").strip()
        return ""

    def read_agents(self) -> str:
        """Read the AGENTS.md file.

        Returns:
            Contents of AGENTS.md, or empty string if not found.
        """
        agents_file = self._antigona_dir / "AGENTS.md"
        if agents_file.exists():
            return agents_file.read_text(encoding="utf-8").strip()
        return ""

    def build_system_prompt(self, personality_name: str = "") -> str:
        """Build a complete system prompt from personality + SOUL.md + AGENTS.md.

        Args:
            personality_name: Personality to use. Empty = current personality.

        Returns:
            Combined system prompt string.
        """
        name = personality_name or self._current_personality
        try:
            persona = self.load_personality(name)
        except PersonalityNotFoundError:
            persona = "Ты — Antigona, AI-агент."

        parts: list[str] = [persona]

        soul = self.read_soul()
        if soul:
            parts.append("")
            parts.append("--- SOUL.md ---")
            parts.append(soul)

        agents = self.read_agents()
        if agents:
            parts.append("")
            parts.append("--- AGENTS.md ---")
            parts.append(agents)

        return "\n".join(parts)


# ─── Global manager instance ──────────────────────────────────────────────────

_manager: PersonalityManager | None = None


def get_personality_manager() -> PersonalityManager:
    """Get or create the global PersonalityManager singleton."""
    global _manager
    if _manager is None:
        _manager = PersonalityManager()
    return _manager


def switch_personality(name: str) -> str:
    """Switch to a different personality.

    Returns:
        The new personality text.
    """
    manager = get_personality_manager()
    return manager.load_personality(name)


def list_personalities() -> dict[str, str]:
    """List available personalities."""
    return get_personality_manager().list_personalities()


def build_context() -> str:
    """Build a full system prompt from current personality + SOUL + AGENTS."""
    return get_personality_manager().build_system_prompt()
