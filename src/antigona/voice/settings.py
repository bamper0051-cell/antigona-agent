"""Voice settings — per-chat TTS toggle."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from antigona.core import paths

logger = logging.getLogger(__name__)

_SETTINGS_PATH = str(paths.voice_settings_file())


class VoiceSettings:
    """Per-chat voice settings persisted to a JSON file.

    Thread-safe for async usage (single-writer via file replace).
    """

    def __init__(self, path: str = _SETTINGS_PATH) -> None:
        self._path = path
        self._settings: dict[int, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path) as f:
                    data = json.load(f)
                # Normalise keys to int
                self._settings = {int(k): v for k, v in data.items()}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load voice settings: %s", exc)
            self._settings = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.warning("Failed to save voice settings: %s", exc)

    def is_enabled(self, chat_id: int) -> bool:
        """Check if TTS is enabled for a chat."""
        return bool(self._settings.get(chat_id, {}).get("enabled", False))

    def set_enabled(self, chat_id: int, enabled: bool) -> None:
        """Enable or disable TTS for a chat."""
        if chat_id not in self._settings:
            self._settings[chat_id] = {}
        self._settings[chat_id]["enabled"] = enabled
        self._save()

    def get_status(self, chat_id: int) -> str:
        """Return a human-readable status string."""
        enabled = self.is_enabled(chat_id)
        return f"{'🔊 Voice ON' if enabled else '🔇 Voice OFF'}"

    def to_dict(self) -> dict[int, dict[str, Any]]:
        return dict(self._settings)


_VOICE_SETTINGS: VoiceSettings | None = None


def get_voice_settings() -> VoiceSettings:
    """Get the module-level singleton VoiceSettings."""
    global _VOICE_SETTINGS
    if _VOICE_SETTINGS is None:
        _VOICE_SETTINGS = VoiceSettings()
    return _VOICE_SETTINGS
