"""Self-configuration of API keys: parse key from text → detect provider →
write to secrets file → verify via test request → apply provider.

Usage:
    from antigona.tools.key_manager import (
        parse_key,
        detect_provider,
        write_key,
        verify_key,
        apply_provider,
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ─── Provider definitions ─────────────────────────────────────────────────────

# provider key: (name, api_key_env_var, secrets_filename, base_url, test_model)
ProviderInfo = tuple[str, str, str, str, str]

PROVIDERS: dict[str, ProviderInfo] = {
    "openrouter": (
        "OpenRouter",
        "OPENROUTER_API_KEY",
        "openrouter.json",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o-mini",
    ),
    "deepseek": (
        "DeepSeek",
        "DEEPSEEK_API_KEY",
        "deepseek.json",
        "https://api.deepseek.com",
        "deepseek-chat",
    ),
    "siliconflow": (
        "SiliconFlow",
        "SILICONFLOW_API_KEY",
        "siliconflow.json",
        "https://api.siliconflow.cn/v1",
        "deepseek-ai/DeepSeek-V3",
    ),
    "openai": (
        "OpenAI",
        "OPENAI_API_KEY",
        "openai.json",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
    ),
    "ollama": (
        "Ollama",
        "OLLAMA_API_KEY",
        "ollama.json",
        "http://127.0.0.1:11434/v1",
        "qwen2.5:1.5b",
    ),
}

# ─── Patterns ─────────────────────────────────────────────────────────────────

# API key patterns known to Antigona
_KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "openrouter": re.compile(r"(sk-or-[a-zA-Z0-9]{20,})"),
    "deepseek": re.compile(r"(sk-[a-f0-9]{32,})"),
    "siliconflow": re.compile(r"(sk-[a-zA-Z0-9]{32,})"),
    "openai": re.compile(r"(sk-[a-zA-Z0-9]{20,})"),
}

# Generic fallback — any sk-* string
_GENERIC_KEY_RE = re.compile(r"(sk-[a-zA-Z0-9._-]{8,})")


def _secrets_dir() -> Path:
    """Get the secrets directory path, creating it if needed."""
    path = Path.home() / ".antigona" / "secrets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_key(text: str) -> str | None:
    """Extract an API key from a message.

    Tries known provider-specific patterns first, then falls back to
    a generic sk-* pattern.

    Args:
        text: User message that may contain an API key.

    Returns:
        The extracted API key, or None if no key was found.
    """
    if not text or not text.strip():
        return None

    # Try provider-specific patterns first (more specific)
    for pattern in _KEY_PATTERNS.values():
        match = pattern.search(text)
        if match:
            return match.group(1)

    # Fallback to generic sk-* pattern
    match = _GENERIC_KEY_RE.search(text)
    if match:
        return match.group(1)

    return None


def detect_provider(key: str) -> str:
    """Detect the provider from an API key prefix.

    Args:
        key: The API key string.

    Returns:
        Provider name string ('openrouter', 'deepseek', 'siliconflow',
        'openai', 'ollama', or 'generic').
    """
    if not key:
        return "generic"

    if key.lower().strip() in ("ollama", "local") or "11434" in key:
        return "ollama"
    elif key.startswith("sk-or-"):
        return "openrouter"
    elif key.startswith("sk-") and len(key) >= 35 and all(c in "abcdef0123456789" for c in key[3:].lower()):
        # DeepSeek keys are sk- followed by 32+ hex chars
        return "deepseek"
    elif key.startswith("sk-"):
        # Generic sk-* — try heuristics
        # SiliconFlow keys are usually longer base64-ish (mixed case, digits)
        # OpenAI keys start with sk- (often with "-proj-" etc.)
        if len(key) >= 35 and re.match(r"^sk-[a-zA-Z0-9]{30,}$", key) and not re.match(r"^sk-[a-f0-9]{32,}$", key.lower()):
            return "siliconflow"
        return "openai"
    else:
        return "generic"


def get_provider_config(provider: str) -> ProviderInfo | None:
    """Get provider configuration from the PROVIDERS dict.

    Args:
        provider: Provider name (lowercase).

    Returns:
        ProviderInfo tuple or None if not found.
    """
    return PROVIDERS.get(provider)


def write_key(provider: str, key: str) -> str:
    """Write an API key to the secrets JSON file for the given provider.

    Args:
        provider: Provider name (e.g. 'openrouter', 'deepseek').
        key: The API key to store.

    Returns:
        Path to the written file as a string.

    Raises:
        ValueError: If the provider is unknown.
    """
    info = get_provider_config(provider)
    if info is None:
        raise ValueError(f"Unknown provider: {provider}. Known: {list(PROVIDERS.keys())}")

    name, env_var, filename, base_url, _ = info
    secrets_path = _secrets_dir() / filename

    # Build the JSON payload — keep existing content, update key
    existing: dict[str, str] = {}
    if secrets_path.exists():
        try:
            existing = json.loads(secrets_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing[env_var] = key
    existing["api_key"] = key
    existing["base_url"] = base_url
    existing["updated_at"] = __import__("datetime").datetime.now().isoformat()

    secrets_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    # Restrict permissions
    secrets_path.chmod(0o600)

    logger.info("Wrote %s key to %s", name, secrets_path)
    return str(secrets_path)


def verify_key(provider: str, key: str, timeout: int = 15) -> tuple[bool, str]:
    """Verify an API key by making a test request to the provider.

    Args:
        provider: Provider name (e.g. 'openrouter', 'deepseek', 'ollama').
        key: The API key to test.
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (success: bool, message: str).
    """
    info = get_provider_config(provider)
    if info is None:
        return False, f"Unknown provider: {provider}"

    name, env_var, filename, base_url, test_model = info

    # Different providers have different test endpoints
    test_urls: dict[str, str] = {
        "openrouter": f"{base_url}/models",
        "deepseek": f"{base_url}/models",
        "siliconflow": f"{base_url}/models",
        "openai": f"{base_url}/models",
        "ollama": f"{base_url}/models",
    }

    url = test_urls.get(provider, f"{base_url}/models")

    try:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if provider != "ollama" and key and key.strip().lower() not in ("none", "null", ""):
            headers["Authorization"] = f"Bearer {key}"
        is_local = provider == "ollama" or "127.0.0.1" in url or "localhost" in url
        with httpx.Client(timeout=timeout, trust_env=not is_local) as client:
            response = client.get(url, headers=headers)

        if response.status_code == 200:
            return True, f"✅ {name}: API key / endpoint works (HTTP 200)."
        elif response.status_code == 401:
            return False, f"❌ {name}: API key rejected (HTTP 401 Unauthorized)."
        elif response.status_code == 403:
            return False, f"❌ {name}: API key lacks permissions (HTTP 403)."
        else:
            return False, f"❌ {name}: unexpected response HTTP {response.status_code}."

    except httpx.ConnectError:
        return False, f"❌ {name}: cannot connect to {base_url}."
    except httpx.TimeoutException:
        return False, f"❌ {name}: request timed out ({timeout}s)."
    except Exception as exc:
        return False, f"❌ {name}: verification error: {exc}"


def apply_provider(provider: str) -> tuple[bool, str]:
    """Make the given provider the active one for conversation engine.

    This updates the conversation engine's provider by setting the appropriate
    environment variable and recreating the provider instance.

    Args:
        provider: Provider name (e.g. 'openrouter', 'deepseek', 'ollama').

    Returns:
        Tuple of (success: bool, message: str).
    """
    info = get_provider_config(provider)
    if info is None:
        return False, f"Unknown provider: {provider}"

    name, env_var, filename, base_url, test_model = info

    if provider == "ollama":
        os.environ["ANTIGONA_PROVIDER"] = "ollama"
        os.environ["PROVIDER_BASE_URL"] = base_url
        logger.info("Applied provider %s (base_url=%s, model=%s)", name, base_url, test_model)
        return True, f"✅ {name} (local) активен и готов к работе."

    secrets_path = _secrets_dir() / filename

    if not secrets_path.exists():
        return False, f"No secrets file for {name} at {secrets_path}."

    try:
        data: dict[str, str] = json.loads(secrets_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"Cannot read secrets file for {name}: {exc}"

    api_key = data.get(env_var) or data.get("api_key") or ""
    if not api_key:
        return False, f"No API key found in secrets file for {name}."

    # Set environment variable for the provider
    os.environ[env_var] = api_key
    os.environ["ANTIGONA_PROVIDER"] = provider
    os.environ["PROVIDER_BASE_URL"] = base_url

    # Also set DEEPSEEK_API_KEY if provider is deepseek (for compatibility)
    if provider == "deepseek":
        os.environ["DEEPSEEK_API_KEY"] = api_key

    # Log the change
    logger.info("Applied provider %s (base_url=%s, model=%s)", name, base_url, test_model)

    return True, f"✅ {name} активен и готов к работе."


def configure_full_keyflow(
    provider: str, key: str
) -> dict[str, Any]:
    """Run the full key configuration flow: write → verify → apply.

    Args:
        provider: Provider name.
        key: API key.

    Returns:
        Dict with keys: success, provider, steps (list of step results).
    """
    steps: list[dict[str, Any]] = []
    overall_success = True

    # Step 1: Write key
    try:
        filepath = write_key(provider, key)
        steps.append({"step": "write", "success": True, "message": f"Key written to {filepath}"})
    except ValueError as exc:
        steps.append({"step": "write", "success": False, "message": str(exc)})
        return {"success": False, "provider": provider, "steps": steps}

    # Step 2: Verify key
    verify_ok, verify_msg = verify_key(provider, key)
    steps.append({"step": "verify", "success": verify_ok, "message": verify_msg})
    if not verify_ok:
        overall_success = False

    # Step 3: Apply provider (only if verify passed)
    if verify_ok:
        apply_ok, apply_msg = apply_provider(provider)
        steps.append({"step": "apply", "success": apply_ok, "message": apply_msg})
        if not apply_ok:
            overall_success = False
    else:
        steps.append({"step": "apply", "success": False, "message": "Skipped — verification failed."})
        overall_success = False

    return {"success": overall_success, "provider": provider, "steps": steps}
