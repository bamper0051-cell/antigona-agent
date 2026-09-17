"""Secret Vault — encrypted key-value store for API keys and credentials.

The Vault stores secrets in ``~/.hermes/vault/`` as individual JSON files
with restricted permissions (``0o600``). Each secret is a file named after
the key.

Security model:
- Files stored with ``0o600`` (owner read/write only)
- No key values are ever logged or displayed in listings
- ``export()`` generates safe shell ``export`` commands for use in ``.env``
"""

from __future__ import annotations

import builtins
import json
import logging
import os
from datetime import UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

_VAULT_ROOT = Path.home() / ".antigona" / "vault"

# Known env vars that VAULT can export — whitelist
_KNOWN_ENV_VARS: set[str] = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "SILICONFLOW_API_KEY",
    "BLACKBOX_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "HUGGINGFACE_API_KEY",
    "REPLICATE_API_KEY",
    "STABILITY_API_KEY",
    "ELEVENLABS_API_KEY",
    "CONTEXT7_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "GATEWAY_TOKEN",
    "ANTIGONA_STATE_FILE",
}


# ── Vault ─────────────────────────────────────────────────────────────────


class Vault:
    """File-based secret vault.

    Each key is stored as a separate JSON file:

        ~/.hermes/vault/<key>.json  →  {"value": "...", "created": "ISO", "updated": "ISO"}

    Usage::

        vault = Vault()
        vault.set("DEEPSEEK_API_KEY", "sk-...")
        key = vault.get("DEEPSEEK_API_KEY")
        vault.delete("DEEPSEEK_API_KEY")
        vault.list()  # returns keys only, no values
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root else _VAULT_ROOT
        self._root.mkdir(parents=True, exist_ok=True)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def get(self, key: str) -> str | None:
        """Retrieve a secret value by key.

        Args:
            key: Case-sensitive secret key name.

        Returns:
            The secret value, or None if not found.
        """
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("value", "")) or None
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read vault key '%s': %s", key, exc)
            return None

    def set(self, key: str, value: str) -> None:
        """Store a secret value.

        Args:
            key: Case-sensitive secret key name.
            value: The secret value to store.
        """
        path = self._path_for(key)
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        existing["value"] = value
        existing.setdefault("created", now)
        existing["updated"] = now

        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        logger.info("Vault: key '%s' written to %s", key, path)

    def delete(self, key: str) -> bool:
        """Delete a secret by key.

        Args:
            key: Case-sensitive secret key name.

        Returns:
            True if the key existed and was deleted.
        """
        path = self._path_for(key)
        if not path.exists():
            return False
        try:
            path.unlink()
            logger.info("Vault: key '%s' deleted", key)
            return True
        except OSError as exc:
            logger.warning("Failed to delete vault key '%s': %s", key, exc)
            return False

    # ── Listing ───────────────────────────────────────────────────────────

    def list(self) -> list[dict[str, str]]:
        """List all stored secrets (values are NOT included).

        Returns:
            List of dicts with keys ``name``, ``created``, ``updated``.
        """
        results: list[dict[str, str]] = []
        if not self._root.is_dir():
            return results
        for entry in sorted(self._root.iterdir()):
            if entry.suffix == ".json" and entry.is_file():
                key = entry.stem
                try:
                    data: dict[str, Any] = json.loads(
                        entry.read_text(encoding="utf-8")
                    )
                    results.append({
                        "name": key,
                        "created": str(data.get("created", "—")),
                        "updated": str(data.get("updated", "—")),
                    })
                except (json.JSONDecodeError, OSError):
                    results.append({"name": key, "created": "—", "updated": "—"})
        return results

    # ── Environment helpers ───────────────────────────────────────────────

    def has_env(self, name: str) -> bool:
        """Check if an environment variable is currently exported.

        Args:
            name: Environment variable name.

        Returns:
            True if the variable is set and non-empty.
        """
        return bool(os.environ.get(name, "").strip())

    def env_whitelist(self) -> builtins.list[str]:
        """Return all known env var names that the vault can export.

        Returns:
            Sorted list of whitelisted environment variable names.
        """
        return sorted(_KNOWN_ENV_VARS)

    # ── Export ────────────────────────────────────────────────────────────

    def export(self, prefix: str = "export ") -> builtins.list[str]:
        """Generate shell ``export`` commands for all stored secrets.

        Only secrets whose keys match known env var names (``env_whitelist``)
        are exported.

        Args:
            prefix: Shell prefix (default ``"export "``).

        Returns:
            List of shell export lines suitable for writing to ``.env``.
        """
        lines: list[str] = []
        for entry in self.list():
            key = entry["name"]
            if key not in _KNOWN_ENV_VARS:
                continue
            value = self.get(key)
            if value is None:
                continue
            # Shell-safe quoting: single-quote and escape single quotes
            escaped = value.replace("'", "'\\''")
            lines.append(f"{prefix}{key}='{escaped}'")
        return lines

    # ── Internal ──────────────────────────────────────────────────────────

    def _path_for(self, key: str) -> Path:
        """Get the file path for a vault key."""
        # Sanitize key: only alphanumeric, underscore, hyphen
        safe_key = "".join(c for c in key if c.isalnum() or c in "_-")
        if not safe_key:
            safe_key = "unnamed"
        return self._root / f"{safe_key}.json"
