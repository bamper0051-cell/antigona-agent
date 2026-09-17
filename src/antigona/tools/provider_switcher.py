"""Provider switcher — switch conversation provider, list available
providers with status, and test the current provider.

Usage:
    from antigona.tools.provider_switcher import (
        switch_to_provider,
        get_available_providers,
        test_current_provider,
    )
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from antigona.conversation.provider_setup import get_default_provider
from antigona.tools.key_manager import PROVIDERS, get_provider_config

logger = logging.getLogger(__name__)


def _secrets_dir() -> Path:
    return Path.home() / ".antigona" / "secrets"


def switch_to_provider(name: str) -> tuple[bool, str]:
    """Switch the conversation engine to use a different provider.

    Reads the API key from the secrets file (or uses local default for Ollama)
    and creates a new provider instance suitable for use with ConversationEngine.

    Args:
        name: Provider name (e.g. 'openrouter', 'deepseek', 'ollama').

    Returns:
        Tuple of (success: bool, message: str).

    Raises:
        ValueError: If the provider is unknown.
    """
    name = name.lower().strip()
    info = get_provider_config(name)
    if info is None:
        known = list(PROVIDERS.keys())
        return False, f"Unknown provider '{name}'. Known: {', '.join(known)}"

    display_name, env_var, filename, base_url, test_model = info

    from antigona.providers.resolver import ProviderResolver

    if name == "ollama":
        success, msg = ProviderResolver.set_active_provider("ollama", test_model)
        provider = ProviderResolver.get_provider()
        if provider is not None:
            try:
                test_messages = [
                    {"role": "user", "content": "Reply OK if you can read this."},
                ]
                response = provider.generate(test_messages, context={"max_tokens": 10})
                logger.info("Provider %s test response: %s", display_name, response[:100])
            except Exception as exc:
                logger.warning("Provider %s test call failed: %s", display_name, exc)
                return False, f"⚠️ {display_name}: local test call failed: {exc}"
        return True, f"✅ Switched to {display_name} (local model: {test_model})."

    secrets_path = _secrets_dir() / filename

    if not secrets_path.exists():
        return False, f"No secrets file for {display_name} at {secrets_path}."

    try:
        data: dict[str, str] = json.loads(secrets_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"Cannot read secrets file: {exc}"

    api_key = data.get(env_var) or data.get("api_key") or ""
    if not api_key:
        return False, f"No API key found for {display_name}."

    # Set environment variables for compatibility & active provider
    os.environ[env_var] = api_key
    if name == "deepseek":
        os.environ["DEEPSEEK_API_KEY"] = api_key

    success, msg = ProviderResolver.set_active_provider(name, test_model)
    provider = ProviderResolver.get_provider()
    if provider is not None:
        try:
            test_messages = [
                {"role": "user", "content": "Reply OK if you can read this."},
            ]
            response = provider.generate(test_messages, context={"max_tokens": 10})
            logger.info("Provider %s test response: %s", display_name, response[:100])
        except Exception as exc:
            logger.warning("Provider %s test call failed: %s", display_name, exc)
            return False, f"⚠️ {display_name}: key file found but test call failed: {exc}"

    return True, f"✅ Switched to {display_name} (model: {test_model})."


def get_available_providers() -> list[dict[str, Any]]:
    """List all known providers with their current status.

    Returns:
        List of dicts with keys: name, display_name, has_secrets (bool),
        has_key (bool), model, base_url, status.
    """
    results: list[dict[str, Any]] = []

    for key, (display_name, env_var, filename, base_url, test_model) in PROVIDERS.items():
        secrets_path = _secrets_dir() / filename
        has_secrets = secrets_path.exists()
        has_key = False
        api_key = ""

        if has_secrets:
            try:
                data: dict[str, str] = json.loads(secrets_path.read_text())
                api_key = data.get(env_var) or data.get("api_key") or ""
                has_key = bool(api_key)
            except (json.JSONDecodeError, OSError):
                pass

        if key == "ollama":
            has_key = True

        # Check if env var is set
        env_set = bool(os.environ.get(env_var)) or (key == "ollama" and os.environ.get("ANTIGONA_PROVIDER") == "ollama")

        # Check if this is the current active provider
        is_active = False
        try:
            current = get_default_provider()
            if current is not None and hasattr(current, "_base_url"):
                is_active = current._base_url.rstrip("/") == base_url.rstrip("/")
        except Exception:
            pass

        if key == "ollama":
            if is_active:
                status = "✅ active"
            else:
                status = "💻 local (ready)"
        elif has_key:
            if is_active:
                status = "✅ active"
            elif env_set:
                status = "🔑 env-loaded"
            else:
                status = "💾 stored"
        elif has_secrets:
            status = "⚠️ no key in file"
        else:
            status = "❌ not configured"

        results.append({
            "name": key,
            "display_name": display_name,
            "has_secrets": has_secrets,
            "has_key": has_key,
            "env_set": env_set,
            "is_active": is_active,
            "model": test_model,
            "base_url": base_url,
            "status": status,
        })

    return results


def test_current_provider(timeout: int = 15) -> tuple[bool, str]:
    """Test the currently active conversation provider.

    Makes a lightweight API call to verify the provider is responsive.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (success: bool, message: str).
    """
    from antigona.providers.resolver import ProviderResolver

    provider = ProviderResolver.get_provider()
    if provider is None:
        return False, "❌ No active LLM provider is configured. Use /providers or /setllm <provider>."

    base_url = getattr(provider, "_base_url", "unknown")
    model = getattr(provider, "_model", "unknown")
    api_key = getattr(provider, "_api_key", "")

    is_ollama = "11434" in base_url or os.environ.get("ANTIGONA_PROVIDER") == "ollama"
    if not api_key and not is_ollama:
        return False, "❌ Active provider has no API key configured."

    try:
        test_messages = [
            {"role": "user", "content": "Reply with just the word OK."},
        ]
        response = provider.generate(
            test_messages,
            context={"max_tokens": 10, "temperature": 0.0},
        )
        return True, f"✅ Provider OK (url={base_url}, model={model}). Response: {response.strip()[:100]}"
    except Exception as exc:
        return False, f"❌ Provider test failed: {exc}"


def format_provider_list(providers: list[dict[str, Any]]) -> str:
    """Format the provider list for display in Telegram.

    Args:
        providers: List from get_available_providers().

    Returns:
        Formatted string.
    """
    lines: list[str] = ["🔌 Доступные провайдеры:", ""]
    for p in providers:
        status_emoji = p["status"]
        lines.append(f"  {status_emoji}  {p['display_name']} ({p['name']})")
        lines.append(f"       Model: {p['model']}")
        lines.append(f"       URL: {p['base_url']}")
        lines.append("")
    lines.append("Команды:")
    lines.append("  /provider list — этот список")
    lines.append("  /provider switch <name> — переключить провайдера")
    lines.append("  /provider test — протестировать текущего")
    return "\n".join(lines)


def get_active_runtime_info() -> Any:
    """Canonical active provider/model runtime info (P10-safe wrapper)."""
    from antigona.providers.resolver import ProviderResolver

    return ProviderResolver.get_active_info()


def set_active_model(provider: str, model: str) -> tuple[bool, str]:
    """Atomically set the active model for a provider (P10-safe wrapper)."""
    from antigona.providers.resolver import ProviderResolver

    return ProviderResolver.set_active_provider(provider, model)

