"""LLM provider setup -- reads env and creates an OpenAI-compatible provider.

Supports active profile selection (e.g. ollama, deepseek, openrouter, siliconflow, openai)
via ProfileRegistry and env vars. Returns configured provider instance or None.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from antigona.providers.openai_compatible import OpenAICompatibleProvider
from antigona.providers.profiles import get_profile_registry
from antigona.providers.resolver import _load_state, resolve_provider_base_url

__all__ = ["get_default_provider"]


def _read_key_from_secrets_file(filename: str, key_name: str = "") -> str:
    if not filename:
        return ""
    secrets_path = Path.home() / ".antigona" / "secrets" / filename
    if not secrets_path.exists():
        return ""
    try:
        data: dict[str, str] = json.loads(secrets_path.read_text())
        return str(
            data.get(key_name, "") or data.get("api_key", "") or ""
        ).strip()
    except (json.JSONDecodeError, OSError):
        return ""


def get_default_provider() -> OpenAICompatibleProvider | None:
    """Get the active/default LLM provider instance.

    Checks active provider setting (`ANTIGONA_PROVIDER` env var) first.
    If `ANTIGONA_PROVIDER` is 'ollama', returns Ollama provider instance
    (base_url="http://127.0.0.1:11434/v1", model="qwen3.5:4b", local=True).

    Otherwise resolves from registered profiles.
    """
    registry = get_profile_registry()
    # Canonical persistent selection wins (split-brain fix: a /setllm performed
    # in the CLI process must be seen by the Gateway process too).
    _st = _load_state()
    _st_provider = (_st.get("provider") or "").strip().lower()
    _st_model = (_st.get("model") or "").strip()
    active_name = _st_provider or (
        os.environ.get("ANTIGONA_PROVIDER")
        or os.environ.get("ACTIVE_PROVIDER")
        or ""
    ).strip().lower()

    if active_name and active_name != "auto":
        profile = registry.get(active_name)
        if profile is not None:
            if profile.local or profile.auth_type == "none":
                model_name = (
                    _st_model
                    or os.environ.get("OLLAMA_MODEL")
                    or os.environ.get("ANTIGONA_MODEL")
                    or os.environ.get("ANTIGONA_MODEL_PRIMARY")
                    or profile.default_model
                )
                return OpenAICompatibleProvider(
                    base_url=resolve_provider_base_url(profile.base_url),
                    api_key="",
                    model=model_name,
                    timeout_seconds=30,
                )
            # Remote provider
            api_key = ""
            for env_var in profile.requires_env or profile.env_vars:
                val = os.environ.get(env_var, "").strip()
                if val:
                    api_key = val
                    break
            if not api_key and profile.secrets_filename:
                api_key = _read_key_from_secrets_file(profile.secrets_filename)
            if api_key or not profile.requires_env:
                model_name = (
                    _st_model
                    or os.environ.get("ANTIGONA_MODEL")
                    or os.environ.get("ANTIGONA_MODEL_PRIMARY")
                    or profile.default_model
                )
                return OpenAICompatibleProvider(
                    base_url=resolve_provider_base_url(profile.base_url),
                    api_key=api_key,
                    model=model_name,
                    timeout_seconds=30,
                )
        # Fail closed: explicit provider selection must NEVER fall through to auto-detection
        return None

    # Check PROVIDER_BASE_URL if it points to Ollama
    base_url_env = os.environ.get("PROVIDER_BASE_URL", "").strip()
    if "11434" in base_url_env or "ollama" in base_url_env:
        ollama_profile = registry.get("ollama")
        default_m = ollama_profile.default_model if ollama_profile else "qwen3.5:4b"
        return OpenAICompatibleProvider(
            base_url=base_url_env or "http://127.0.0.1:11434/v1",
            api_key="",
            model=os.environ.get("OLLAMA_MODEL") or default_m,
            timeout_seconds=30,
        )

    # Auto-detection among profiles (deepseek -> openrouter -> siliconflow -> openai -> ollama)
    for name in ["deepseek", "openrouter", "siliconflow", "openai"]:
        profile = registry.get(name)
        if profile is None:
            continue
        api_key = ""
        for env_var in profile.requires_env or profile.env_vars:
            val = os.environ.get(env_var, "").strip()
            if val:
                api_key = val
                break
        if not api_key and profile.secrets_filename:
            api_key = _read_key_from_secrets_file(profile.secrets_filename)
        if api_key:
            return OpenAICompatibleProvider(
                base_url=profile.base_url,
                api_key=api_key,
                model=profile.default_model,
                timeout_seconds=30,
            )

    # Local fallback to Ollama profile if registered
    ollama_profile = registry.get("ollama")
    if ollama_profile is not None:
        return OpenAICompatibleProvider(
            base_url=ollama_profile.base_url,
            api_key="",
            model=os.environ.get("OLLAMA_MODEL") or ollama_profile.default_model,
            timeout_seconds=30,
        )

    return None

