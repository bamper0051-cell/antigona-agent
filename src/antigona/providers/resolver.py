"""ProviderResolver — Single canonical source of truth for LLM provider & model runtime status.

Coordinates ProfileRegistry, a persistent canonical state file, active environment
settings, and provider instantiation so /setllm, /model, ContextBuilder, and
DialogueEngine all share the exact same runtime truth across CLI and Gateway
processes (split-brain fix: the switch persists to a file that every process reads).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from antigona.providers.base import BaseProvider
from antigona.providers.profiles import get_profile_registry

logger = logging.getLogger(__name__)

_STATE_FILE_NAME = "provider_state.json"


def _state_file_path() -> Path:
    """Canonical persistent provider-state file (shared across CLI & Gateway).

    Runtime-writable: governed by :func:`antigona.core.paths.provider_state_file`
    (``ANTIGONA_PROVIDER_STATE_FILE`` > ``<state_root>/provider_state.json``).
    """
    from antigona.core import paths

    return paths.provider_state_file()


def resolve_provider_base_url(profile_base_url: str) -> str:
    """Return the effective API base URL for an explicit provider profile.

    ``PROVIDER_BASE_URL`` is honored only when it targets the same host as the
    profile. A mismatched override (e.g. state=openrouter while
    ``PROVIDER_BASE_URL=https://api.deepseek.com``) used to send the wrong API
    key to the wrong host and fail with HTTP 401.
    """
    override = (os.environ.get("PROVIDER_BASE_URL") or "").strip()
    if not override:
        return profile_base_url

    def _host(url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return (parsed.hostname or "").lower()

    ov_host = _host(override)
    pr_host = _host(profile_base_url)
    if ov_host and pr_host and ov_host == pr_host:
        return override.rstrip("/")
    return profile_base_url


def _load_state() -> dict[str, str]:
    """Read the persisted canonical provider selection. Never raises."""
    path = _state_file_path()
    try:
        if not path.exists():
            return {}
        data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_state(provider: str, model: str | None = None) -> None:
    """Persist the canonical provider selection. Best-effort."""
    path = _state_file_path()
    state = _load_state()
    state["provider"] = provider
    if model:
        state["model"] = model
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Failed to persist provider state to %s: %s", path, exc)


@dataclass(frozen=True)
class ProviderRuntimeInfo:
    """Immutable snapshot of the active LLM provider runtime state."""

    provider_name: str
    display_name: str
    model_name: str
    base_url: str
    endpoint_class: str  # "local" | "remote" | "unresolved"
    status: str          # "active" | "unresolved"

    def to_system_block(self) -> str:
        """Format the server-generated immutable runtime environment block for system prompts."""
        p_name = self.display_name if self.status == "active" else "unresolved"
        m_name = self.model_name if self.status == "active" else "unresolved"
        ep_class = self.endpoint_class if self.status == "active" else "unresolved"
        st = self.status

        return (
            "--- RUNTIME ENVIRONMENT ---\n"
            f"Agent: Antigona\n"
            f"LLM provider: {p_name}\n"
            f"LLM model: {m_name}\n"
            f"Execution endpoint class: {ep_class}\n"
            f"Provider status: {st}\n\n"
            "These values are supplied by Antigona runtime.\n"
            "When the user asks what provider or model is currently active,\n"
            "answer using these runtime values.\n"
            "Do not guess another provider or model.\n"
            "Do not claim that this information is unavailable."
        )


class ProviderResolver:
    """Canonical resolver for active LLM provider and model state."""

    _cached_provider: BaseProvider | None = None
    _cached_key: tuple[str, str, str, str] | None = None

    @classmethod
    def _active_name(cls) -> str:
        """Resolve the explicitly selected provider name: state file > env."""
        state = _load_state()
        state_provider = (state.get("provider") or "").strip().lower()
        if state_provider:
            return state_provider
        return (
            os.environ.get("ANTIGONA_PROVIDER")
            or os.environ.get("ACTIVE_PROVIDER")
            or ""
        ).strip().lower()

    @classmethod
    def _model_override(cls, provider_name: str) -> str | None:
        """Model override: state file wins over env for the active provider.

        Rejects a state/env value that is empty or identical to the provider
        name (a common mis-set from `/setllm deepseek` without a model) so we
        fall through to ANTIGONA_MODEL_PRIMARY / profile.default_model instead
        of calling the API with e.g. model=\"deepseek\" (HTTP 400).
        """
        def _usable(model: str | None) -> str | None:
            m = (model or "").strip()
            if not m:
                return None
            if provider_name and m.lower() == provider_name.lower():
                return None
            return m

        state = _load_state()
        state_model = _usable(state.get("model"))
        if state_model:
            return state_model
        if provider_name == "ollama":
            return _usable(
                os.environ.get("OLLAMA_MODEL")
                or os.environ.get("ANTIGONA_MODEL")
                or os.environ.get("ANTIGONA_MODEL_PRIMARY")
            )
        return _usable(
            os.environ.get("ANTIGONA_MODEL")
            or os.environ.get("ANTIGONA_MODEL_PRIMARY")
        )

    @classmethod
    def get_active_info(cls) -> ProviderRuntimeInfo:
        """Inspect the current runtime configuration and return ProviderRuntimeInfo."""
        registry = get_profile_registry()
        active_name = cls._active_name()

        if active_name in ("none", "disabled", "off", "unresolved"):
            return ProviderRuntimeInfo(
                provider_name="unresolved",
                display_name="unresolved",
                model_name="unresolved",
                base_url="",
                endpoint_class="unresolved",
                status="unresolved",
            )

        # 1. If explicit provider is set (state file or env)
        if active_name and active_name != "auto":
            profile = registry.get(active_name)
            if profile is not None:
                display_name = profile.display_name
                is_local = profile.local or profile.auth_type == "none"
                has_key = True
                if not is_local and profile.requires_env:
                    from antigona.conversation.provider_setup import _read_key_from_secrets_file

                    api_key = ""
                    for env_var in profile.requires_env or profile.env_vars:
                        val = os.environ.get(env_var, "").strip()
                        if val:
                            api_key = val
                            break
                    if not api_key and profile.secrets_filename:
                        api_key = _read_key_from_secrets_file(profile.secrets_filename)
                    if not api_key:
                        has_key = False

                if has_key:
                    model_name = (
                        cls._model_override(active_name)
                        or profile.default_model
                    )
                    base_url = resolve_provider_base_url(profile.base_url)
                    endpoint_class = "local" if is_local else "remote"
                    return ProviderRuntimeInfo(
                        provider_name=active_name,
                        display_name=display_name,
                        model_name=model_name,
                        base_url=base_url,
                        endpoint_class=endpoint_class,
                        status="active",
                    )
            # Fail closed: explicit provider selection must NEVER fall through to auto-detection
            return ProviderRuntimeInfo(
                provider_name=active_name,
                display_name=active_name if profile is None else profile.display_name,
                model_name="unresolved",
                base_url="",
                endpoint_class="unresolved",
                status="unresolved",
            )

        # 2. Check PROVIDER_BASE_URL pointing to Ollama
        base_url_env = os.environ.get("PROVIDER_BASE_URL", "").strip()
        if "11434" in base_url_env or "ollama" in base_url_env:
            ollama_profile = registry.get("ollama")
            default_m = ollama_profile.default_model if ollama_profile else "qwen3.5:4b"
            model_name = cls._model_override("ollama") or default_m
            return ProviderRuntimeInfo(
                provider_name="ollama",
                display_name="Ollama",
                model_name=model_name,
                base_url=base_url_env or "http://127.0.0.1:11434/v1",
                endpoint_class="local",
                status="active",
            )

        # 3. Auto-detection among profiles with API keys
        from antigona.conversation.provider_setup import _read_key_from_secrets_file

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
                model_name = cls._model_override(name) or profile.default_model
                return ProviderRuntimeInfo(
                    provider_name=name,
                    display_name=profile.display_name,
                    model_name=model_name,
                    base_url=profile.base_url,
                    endpoint_class="remote",
                    status="active",
                )

        # 4. Local fallback to Ollama profile if registered
        ollama_profile = registry.get("ollama")
        if ollama_profile is not None:
            model_name = cls._model_override("ollama") or ollama_profile.default_model
            return ProviderRuntimeInfo(
                provider_name="ollama",
                display_name="Ollama",
                model_name=model_name,
                base_url=ollama_profile.base_url,
                endpoint_class="local",
                status="active",
            )

        # Fail closed
        return ProviderRuntimeInfo(
            provider_name="unresolved",
            display_name="unresolved",
            model_name="unresolved",
            base_url="",
            endpoint_class="unresolved",
            status="unresolved",
        )

    @classmethod
    def get_provider(cls) -> BaseProvider | None:
        """Get or create the active BaseProvider instance based on canonical runtime info."""
        info = cls.get_active_info()
        if info.status != "active":
            return None

        # State-file mtime in the cache key so a switch performed in ANOTHER
        # process (CLI) invalidates this process's (Gateway) cached provider.
        state_mtime = ""
        try:
            state_path = _state_file_path()
            if state_path.exists():
                state_mtime = str(int(state_path.stat().st_mtime))
        except OSError:
            pass

        cache_key = (info.provider_name, info.model_name, info.base_url, state_mtime)
        if cls._cached_provider is not None and cls._cached_key == cache_key:
            return cls._cached_provider

        cls.clear_cache()

        from antigona.conversation.provider_setup import get_default_provider

        provider = get_default_provider()
        if provider is not None:
            cls._cached_provider = provider
            cls._cached_key = cache_key
        return provider

    @classmethod
    def set_active_provider(cls, provider_name: str, model_name: str | None = None) -> tuple[bool, str]:
        """Atomically set active provider and model in runtime state (env + persistent file)."""
        provider_name = provider_name.lower().strip()
        registry = get_profile_registry()
        profile = registry.get(provider_name)
        if profile is None and provider_name != "auto":
            known = [p.name for p in registry.list()]
            return False, f"Unknown provider '{provider_name}'. Known: {', '.join(known)}"

        os.environ["ANTIGONA_PROVIDER"] = provider_name
        if profile is not None:
            os.environ["PROVIDER_BASE_URL"] = profile.base_url
        # Prefer explicit model; never persist provider-name-as-model.
        resolved_model: str | None = None
        if model_name:
            model_name = model_name.strip()
            if model_name and model_name.lower() != provider_name:
                resolved_model = model_name
        if resolved_model is None and profile is not None:
            resolved_model = profile.default_model
        if resolved_model is None:
            resolved_model = (
                os.environ.get("ANTIGONA_MODEL")
                or os.environ.get("ANTIGONA_MODEL_PRIMARY")
                or None
            )
        if resolved_model:
            if provider_name == "ollama":
                os.environ["OLLAMA_MODEL"] = resolved_model
            os.environ["ANTIGONA_MODEL"] = resolved_model

        # Persist canonical selection so OTHER processes (Gateway) see it too.
        _save_state(provider_name, resolved_model)

        cls.clear_cache()
        info = cls.get_active_info()
        disp = info.display_name if info.status == "active" else provider_name
        mod = info.model_name
        return True, f"✅ Switched to {disp} (model: {mod})."

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached provider instance."""
        if cls._cached_provider is not None and hasattr(cls._cached_provider, "close"):
            try:
                cls._cached_provider.close()
            except Exception:
                pass
        cls._cached_provider = None
        cls._cached_key = None
