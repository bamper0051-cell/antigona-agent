"""Provider Profile — declarative provider configuration with ProfileRegistry.

Each **ProviderProfile** describes a single LLM/API provider — its
environment variables, base URL, authentication mode, supported models,
and fallback behaviour. Profiles are registered in a **ProfileRegistry**
which can resolve a profile name to a concrete configuration dict.

This complements the existing runtime providers (``BaseProvider`` /
``OpenAICompatibleProvider`` in ``antigona.providers.base``) by providing
a **static metadata layer** that the rest of the system (CLI commands,
Telegram bot, setup wizards) can query without instantiating a provider.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antigona.providers.openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


# ── Provider Profile ──────────────────────────────────────────────────────


@dataclass
class ProviderProfile:
    """Declarative metadata for an LLM/API provider.

    Attributes:
        name: Short lowercase identifier (e.g. ``"deepseek"``, ``"openrouter"``).
        display_name: Human-readable name (e.g. ``"DeepSeek"``, ``"OpenRouter"``).
        env_vars: Dict of ``{env_var_name: description}`` required by this provider.
        base_url: Default API base URL.
        auth_type: Authentication method — ``"bearer"``, ``"header"``, ``"none"``.
        api_mode: API protocol — ``"openai"``, ``"anthropic"``, ``"custom"``.
        fallback_models: Ordered list of model names to fall back on.
        default_model: Default model name to use when none is specified.
        secrets_filename: Base name for the secrets JSON file (without path).
        requires_env: List of environment variables that must be set.
        test_model: Model to use for connectivity verification.
    """

    name: str
    display_name: str
    env_vars: dict[str, str] = field(default_factory=dict)
    base_url: str = ""
    auth_type: str = "bearer"
    api_mode: str = "openai"
    fallback_models: list[str] = field(default_factory=list)
    default_model: str = ""
    secrets_filename: str = ""
    requires_env: list[str] = field(default_factory=list)
    test_model: str = ""
    local: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (safe for display/logging)."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "auth_type": self.auth_type,
            "api_mode": self.api_mode,
            "default_model": self.default_model,
            "test_model": self.test_model,
            "fallback_models": self.fallback_models,
            "requires_env": self.requires_env,
            "env_var_keys": list(self.env_vars.keys()),
            "local": self.local,
        }

    def resolve_config(
        self, api_key: str = "", overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Resolve to a configuration dict that can instantiate a provider.

        Args:
            api_key: The API key to use (empty = try env/secrets).
            overrides: Optional overrides for ``base_url``, ``model``, ``timeout``.

        Returns:
            Configuration dict with keys: ``base_url``, ``api_key``, ``model``,
            ``timeout_seconds``, ``auth_type``.
        """
        cfg: dict[str, Any] = {
            "base_url": self.base_url,
            "api_key": api_key,
            "model": self.default_model or self.test_model or "",
            "timeout_seconds": 30,
            "auth_type": self.auth_type,
            "local": self.local,
        }
        if overrides:
            cfg.update(overrides)
        return cfg

    def create_provider(
        self, api_key: str = "", overrides: dict[str, Any] | None = None
    ) -> OpenAICompatibleProvider:
        """Create an ``OpenAICompatibleProvider`` from this profile.

        Args:
            api_key: API key (empty = try to read from env or secrets).
            overrides: Optional overrides (base_url, model, timeout).

        Returns:
            Configured ``OpenAICompatibleProvider`` instance.
        """
        cfg = self.resolve_config(api_key=api_key, overrides=overrides)
        return OpenAICompatibleProvider(
            base_url=str(cfg.get("base_url", self.base_url)),
            api_key=str(cfg.get("api_key", "")),
            model=str(cfg.get("model", self.default_model or "")),
            timeout_seconds=int(cfg.get("timeout_seconds", 30)),
        )


# ── Built-in provider profiles ────────────────────────────────────────────

_BUILTIN_PROFILES: dict[str, ProviderProfile] = {
    "ollama": ProviderProfile(
        name="ollama",
        display_name="Ollama",
        env_vars={},
        base_url="http://127.0.0.1:11434/v1",
        auth_type="none",
        api_mode="openai",
        default_model="qwen3.5:4b",
        fallback_models=["qwen2.5:7b", "llama3.2:3b", "deepseek-r1:7b"],
        secrets_filename="",
        requires_env=[],
        test_model="qwen3.5:4b",
        local=True,
    ),
    "deepseek": ProviderProfile(
        name="deepseek",
        display_name="DeepSeek",
        env_vars={"DEEPSEEK_API_KEY": "DeepSeek API key"},
        base_url="https://api.deepseek.com",
        auth_type="bearer",
        api_mode="openai",
        default_model="deepseek-v4-flash",
        fallback_models=["deepseek-chat", "deepseek-v3"],
        secrets_filename="deepseek.json",
        requires_env=["DEEPSEEK_API_KEY"],
        test_model="deepseek-v4-flash",
    ),
    "openrouter": ProviderProfile(
        name="openrouter",
        display_name="OpenRouter",
        env_vars={"OPENROUTER_API_KEY": "OpenRouter API key"},
        base_url="https://openrouter.ai/api/v1",
        auth_type="bearer",
        api_mode="openai",
        default_model="openai/gpt-4o-mini",
        fallback_models=["openai/gpt-4o", "anthropic/claude-sonnet"],
        secrets_filename="openrouter.json",
        requires_env=["OPENROUTER_API_KEY"],
        test_model="openai/gpt-4o-mini",
    ),
    "blackbox": ProviderProfile(
        name="blackbox",
        display_name="Blackbox AI",
        env_vars={"BLACKBOX_API_KEY": "Blackbox API key"},
        base_url="https://api.blackbox.ai",
        auth_type="bearer",
        api_mode="openai",
        default_model="blackbox-v4",
        fallback_models=["blackbox-pro", "blackbox-lite"],
        secrets_filename="blackbox.json",
        requires_env=["BLACKBOX_API_KEY"],
        test_model="blackbox-v4",
    ),
    "siliconflow": ProviderProfile(
        name="siliconflow",
        display_name="SiliconFlow",
        env_vars={"SILICONFLOW_API_KEY": "SiliconFlow API key"},
        base_url="https://api.siliconflow.cn/v1",
        auth_type="bearer",
        api_mode="openai",
        default_model="deepseek-ai/DeepSeek-V3",
        fallback_models=["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B"],
        secrets_filename="siliconflow.json",
        requires_env=["SILICONFLOW_API_KEY"],
        test_model="deepseek-ai/DeepSeek-V3",
    ),
    "openai": ProviderProfile(
        name="openai",
        display_name="OpenAI",
        env_vars={"OPENAI_API_KEY": "OpenAI API key"},
        base_url="https://api.openai.com/v1",
        auth_type="bearer",
        api_mode="openai",
        default_model="gpt-4o-mini",
        fallback_models=["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"],
        secrets_filename="openai.json",
        requires_env=["OPENAI_API_KEY"],
        test_model="gpt-4o-mini",
    ),
}

# ── Profile Registry ──────────────────────────────────────────────────────


class ProfileRegistryError(Exception):
    """Raised on profile registry errors."""


class ProfileRegistry:
    """Registry for ``ProviderProfile`` metadata.

    Provides ``list``, ``get``, ``resolve`` operations. Profiles can be
    registered programmatically or from YAML/JSON files.

    Usage::

        registry = ProfileRegistry()
        registry.register_profile(deepseek_profile)
        profile = registry.get("deepseek")
        config = registry.resolve("deepseek", api_key="sk-...")
    """

    def __init__(self) -> None:
        self._profiles: dict[str, ProviderProfile] = {}

    def register_profile(self, profile: ProviderProfile) -> str:
        """Register a provider profile.

        Args:
            profile: The ``ProviderProfile`` instance.

        Returns:
            The profile name.
        """
        self._profiles[profile.name] = profile
        logger.info(
            "Profile registered: '%s' (%s)", profile.name, profile.display_name
        )
        return profile.name

    def unregister_profile(self, name: str) -> bool:
        """Unregister a profile.

        Returns:
            True if found and removed.
        """
        if name in self._profiles:
            del self._profiles[name]
            logger.info("Profile unregistered: '%s'", name)
            return True
        return False

    def get(self, name: str) -> ProviderProfile | None:
        """Get a profile by name."""
        return self._profiles.get(name)

    def list(self) -> list[ProviderProfile]:
        """List all registered profiles."""
        return list(self._profiles.values())

    def resolve(
        self,
        name: str,
        api_key: str = "",
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve a profile name to a full configuration dict.

        Looks up the profile and merges secrets from the vault or env.

        Args:
            name: Profile name.
            api_key: Explicit API key (overrides env/secrets).
            overrides: Optional overrides (base_url, model, timeout).

        Returns:
            Configuration dict with all keys needed to create a provider.

        Raises:
            ProfileRegistryError: If the profile name is not found.
        """
        profile = self.get(name)
        if profile is None:
            raise ProfileRegistryError(
                f"Unknown profile '{name}'. Registered: {list(self._profiles)}"
            )

        # Resolve API key: explicit > env > vault
        resolved_key = api_key
        if not resolved_key:
            for env_var in profile.env_vars:
                import os

                val = os.environ.get(env_var, "").strip()
                if val:
                    resolved_key = val
                    break

        if not resolved_key:
            # Try the vault
            try:
                from antigona.secrets.vault import Vault

                vault = Vault()
                for env_var in profile.env_vars:
                    vault_val = vault.get(env_var)
                    if vault_val:
                        resolved_key = vault_val
                        break
            except Exception:
                pass

        cfg = profile.resolve_config(api_key=resolved_key, overrides=overrides)
        return cfg

    def load_profiles_from_json(self, path: str | Path) -> int:
        """Load profiles from a JSON file.

        Expected format: list of dicts with keys matching ``ProviderProfile`` fields.

        Args:
            path: Path to JSON file.

        Returns:
            Number of profiles loaded.
        """
        path_resolved = Path(path)
        if not path_resolved.exists():
            logger.warning("Profiles file not found: %s", path_resolved)
            return 0
        try:
            data: list[dict[str, Any]] = json.loads(
                path_resolved.read_text(encoding="utf-8")
            )
            count = 0
            for item in data:
                name = item.pop("name", None)
                if not name:
                    continue
                profile = ProviderProfile(name=name, **item)
                self.register_profile(profile)
                count += 1
            logger.info("Loaded %d profile(s) from %s", count, path_resolved)
            return count
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.error("Failed to load profiles from %s: %s", path_resolved, exc)
            return 0

    def count(self) -> int:
        """Number of registered profiles."""
        return len(self._profiles)


# ── Convenience: register a single profile ────────────────────────────────

def register_profile(profile: ProviderProfile) -> str:
    """Register a profile in the default module-level registry.

    This is the quick-import function for use by scripts and tests::

        from antigona.providers.profiles import register_profile, ProviderProfile

        p = ProviderProfile(name="my-provider", display_name="My Provider", ...)
        register_profile(p)

    Returns:
        The profile name.
    """
    return _get_default_profile_registry().register_profile(profile)


# ── Module-level singleton ────────────────────────────────────────────────

_PROFILE_REGISTRY: ProfileRegistry | None = None


def get_profile_registry() -> ProfileRegistry:
    """Get the module-level singleton ``ProfileRegistry``.

    Initialised with all built-in profiles on first call.
    """
    global _PROFILE_REGISTRY
    if _PROFILE_REGISTRY is None:
        _PROFILE_REGISTRY = ProfileRegistry()
        # Register built-in profiles
        for _name, profile in _BUILTIN_PROFILES.items():
            _PROFILE_REGISTRY.register_profile(profile)
        logger.debug(
            "Profile registry initialized with %d built-in profiles",
            len(_BUILTIN_PROFILES),
        )
    return _PROFILE_REGISTRY


def _get_default_profile_registry() -> ProfileRegistry:
    """Internal: get the singleton registry (same as ``get_profile_registry()``)."""
    return get_profile_registry()
