"""Core plugin data model — Plugin, PluginRegistry, PluginLoader, PluginContext.

Each plugin is a directory (or pip-installed package) with a ``plugin.yaml``
manifest. Plugins can register tools, hooks, commands, and skills at runtime
through a **PluginContext** that mediates safe access to the Antigona system.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# ── PluginLlmAccess — sandboxed LLM access for plugins ────────────────────


class PluginLlmAccess:
    """Sandboxed LLM access for plugins — thin wrapper over the active provider.

    Plugins get a **read-only** facade so they cannot replace the provider
    or access raw credentials.
    """

    def __init__(self, provider_ref: Any) -> None:
        self._provider = provider_ref

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        """Send a completion request through the active provider."""
        if self._provider is None:
            raise RuntimeError("No active LLM provider available")
        return cast(str, self._provider.generate(
            messages,
            context={"max_tokens": max_tokens, "temperature": temperature},
        ))


# ── Plugin dataclass ──────────────────────────────────────────────────────


@dataclass
class Plugin:
    """Representation of a loaded plugin.

    Attributes:
        name: Unique plugin name.
        kind: Plugin category — ``"tool"``, ``"hook"``, ``"platform"``, ``"provider"``.
        version: Semantic version string.
        description: One-line summary.
        manifest_path: Absolute path to the ``plugin.yaml`` manifest file.
        manifest: Raw parsed manifest dict.
        ctx: Runtime context handle the plugin uses to register its capabilities.
    """

    name: str
    kind: str = "tool"
    version: str = "0.1.0"
    description: str = ""
    manifest_path: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    ctx: PluginContext | None = None


# ── PluginContext — runtime handle for plugins ────────────────────────────


class PluginContext:
    """Runtime context given to a plugin on load.

    Through this context a plugin can register tools, hooks, commands, and
    skills into the Antigona runtime. Every operation returns an opaque
    registration id so the system can unregister them on unload.
    """

    def __init__(self, plugin_name: str) -> None:
        self._plugin_name = plugin_name
        self._tools: dict[str, dict[str, Any]] = {}
        self._hooks: dict[str, tuple[str, Callable[..., Awaitable[None]]]] = {}
        self._commands: dict[str, tuple[str, Callable[..., Awaitable[None]]]] = {}
        self._skills: dict[str, str] = {}
        self._llm_provider: PluginLlmAccess | None = None
        self._registrations: list[str] = field(default_factory=list)

    # ── LLM access ────────────────────────────────────────────────────────

    @property
    def llm(self) -> PluginLlmAccess:
        """Access the active LLM provider through a sandboxed wrapper."""
        if self._llm_provider is None:
            # Lazily resolve the default provider
            try:
                from antigona.conversation.provider_setup import get_default_provider

                provider = get_default_provider()
            except Exception:
                provider = None
            self._llm_provider = PluginLlmAccess(provider)
        return self._llm_provider

    # ── Tool registration ─────────────────────────────────────────────────

    def register_tool(self, schema: dict[str, Any], handler: Callable[..., Any]) -> str:
        """Register a tool with an OpenAI-compatible function schema.

        Args:
            schema: JSON Schema dict (``name``, ``description``, ``parameters``).
            handler: Synchronous or async callable that implements the tool.

        Returns:
            Opaque registration id.
        """
        tool_name = schema.get("name", f"plugin_{self._plugin_name}_{len(self._tools)}")
        reg_id = f"tool:{self._plugin_name}:{tool_name}"
        self._tools[reg_id] = {"schema": schema, "handler": handler}
        logger.info("Plugin '%s' registered tool '%s'", self._plugin_name, tool_name)
        return reg_id

    # ── Hook registration ─────────────────────────────────────────────────

    def register_hook(
        self, event: str, fn: Callable[..., Awaitable[None]]
    ) -> str:
        """Register a hook that fires on a named event pattern.

        Args:
            event: Glob-style event pattern (e.g. ``"message:*"``).
            fn: Async callable ``(event_name, context_dict) -> None``.

        Returns:
            Opaque registration id.
        """
        reg_id = f"hook:{self._plugin_name}:{event}"
        self._hooks[reg_id] = (event, fn)

        # Try to wire into the global hook registry immediately
        try:
            from antigona.hooks import Hook, get_hook_registry

            hook_name = f"_plugin_{self._plugin_name}_{event.replace(':', '_')}"
            registry = get_hook_registry()
            registry.register(
                Hook(
                    name=hook_name,
                    event_pattern=event,
                    handler=fn,
                    description=f"Plugin '{self._plugin_name}' hook for '{event}'",
                )
            )
        except Exception:
            logger.warning(
                "Plugin '%s': could not wire hook '%s' into global registry",
                self._plugin_name,
                event,
            )

        logger.info(
            "Plugin '%s' registered hook for event '%s'",
            self._plugin_name,
            event,
        )
        return reg_id

    # ── Command registration ──────────────────────────────────────────────

    def register_command(
        self, name: str, handler: Callable[..., Awaitable[None]], desc: str = ""
    ) -> str:
        """Register a Telegram slash command handler.

        Args:
            name: Command name without leading ``/``.
            handler: Async callable ``(Message) -> None``.
            desc: Help text description.

        Returns:
            Opaque registration id.
        """
        reg_id = f"cmd:{self._plugin_name}:{name}"
        self._commands[reg_id] = (desc, handler)
        logger.info(
            "Plugin '%s' registered command '/%s': %s",
            self._plugin_name,
            name,
            desc,
        )
        return reg_id

    # ── Skill registration ────────────────────────────────────────────────

    def register_skill(self, content: str) -> str:
        """Register a skill (SKILL.md) string in the skills directory.

        Args:
            content: Complete SKILL.md content (frontmatter + body).

        Returns:
            Opaque registration id.
        """
        # Extract name from frontmatter
        name_match = re.search(r"^name:\s*(\S+)", content, re.MULTILINE)
        skill_name = name_match.group(1) if name_match else f"plugin_{self._plugin_name}"
        skill_path = Path.home() / ".antigona" / "skills" / f"{skill_name}.skill" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(content, encoding="utf-8")
        reg_id = f"skill:{self._plugin_name}:{skill_name}"
        self._skills[reg_id] = skill_name
        logger.info(
            "Plugin '%s' registered skill '%s' at %s",
            self._plugin_name,
            skill_name,
            skill_path,
        )
        return reg_id

    # ── Cleanup ───────────────────────────────────────────────────────────

    def unregister_all(self) -> None:
        """Remove all registrations made by this plugin.

        Called automatically by ``PluginRegistry.unregister()``.
        """
        for reg_id in list(self._tools):
            del self._tools[reg_id]
        for reg_id in list(self._hooks):
            # Remove from global hook registry
            hook_name = reg_id.replace(":", "_").replace(".", "_")
            try:
                from antigona.hooks import get_hook_registry

                registry = get_hook_registry()
                registry.unregister(hook_name)
            except Exception:
                pass
            del self._hooks[reg_id]
        for reg_id in list(self._commands):
            del self._commands[reg_id]
        for reg_id, skill_name in list(self._skills.items()):
            skill_path = Path.home() / ".antigona" / "skills" / f"{skill_name}.skill" / "SKILL.md"
            try:
                if skill_path.exists():
                    skill_path.unlink()
            except OSError:
                pass
            del self._skills[reg_id]
        logger.info("Plugin '%s': unregistered all resources", self._plugin_name)


# ── Plugin Registry ───────────────────────────────────────────────────────


class PluginRegistry:
    """Central registry for plugins.

    Thread-safe for read operations (``list``, ``get``). Write operations
    (``register``, ``unregister``) should be serialised.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> str:
        """Register a plugin.

        If a plugin with the same name exists, it is unregistered first.

        Returns:
            The plugin name.
        """
        if plugin.name in self._plugins:
            logger.warning(
                "Plugin '%s' already registered. Replacing.", plugin.name
            )
            self.unregister(plugin.name)
        self._plugins[plugin.name] = plugin
        logger.info(
            "Plugin registered: '%s' (kind=%s, v%s)",
            plugin.name,
            plugin.kind,
            plugin.version,
        )
        return plugin.name

    def unregister(self, name: str) -> bool:
        """Unregister a plugin and clean up its resources.

        Returns:
            True if the plugin was found and removed.
        """
        plugin = self._plugins.pop(name, None)
        if plugin is None:
            return False
        if plugin.ctx is not None:
            plugin.ctx.unregister_all()
        logger.info("Plugin unregistered: '%s'", name)
        return True

    def get(self, name: str) -> Plugin | None:
        """Look up a plugin by name."""
        return self._plugins.get(name)

    def list(self, kind: str | None = None) -> list[Plugin]:
        """List registered plugins, optionally filtered by *kind*."""
        if kind is None:
            return list(self._plugins.values())
        return [p for p in self._plugins.values() if p.kind == kind]

    def count(self) -> int:
        """Number of registered plugins."""
        return len(self._plugins)

    def clear(self) -> None:
        """Unregister all plugins."""
        for name in list(self._plugins):
            self.unregister(name)


# ── Plugin Loader ─────────────────────────────────────────────────────────


class PluginLoadError(Exception):
    """Raised when a plugin cannot be loaded."""


class PluginLoader:
    """Discovers and loads plugins from filesystem directories.

    Looks for ``plugin.yaml`` manifests in:

    1. ``~/.antigona/plugins/<name>/`` (auto-load path)
    2. Explicit directories passed to ``load_plugin()`` / ``discover_plugins()``
    3. Installed pip packages with ``antigona_plugins`` entry point (optional)
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        # Unified Paths API (ADR-003): the brain resolves plugin directories via
        # paths.owner_dir(), so the loader must resolve its root the same way —
        # otherwise load, list and disable disagree about where plugins live.
        from antigona.core.paths import owner_dir

        self._auto_load_path = owner_dir() / "plugins"

    def _parse_manifest(self, manifest_path: Path) -> dict[str, Any] | None:
        """Parse a ``plugin.yaml`` manifest file."""
        if not manifest_path.exists():
            logger.warning("Manifest not found: %s", manifest_path)
            return None
        try:
            import yaml  # type: ignore[import-untyped]

            text = manifest_path.read_text(encoding="utf-8")
            manifest: dict[str, Any] = yaml.safe_load(text) or {}
            return manifest
        except ImportError:
            # Fallback: minimal YAML parsing (name: value only)
            return self._parse_manifest_minimal(manifest_path)
        except Exception as exc:
            logger.error("Failed to parse manifest %s: %s", manifest_path, exc)
            return None

    def _parse_manifest_minimal(self, manifest_path: Path) -> dict[str, Any] | None:
        """Minimal fallback YAML parser when PyYAML is not installed."""
        try:
            manifest: dict[str, Any] = {}
            text = manifest_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    key, _, value = line.partition(":")
                    manifest[key.strip()] = value.strip().strip('"').strip("'")
            return manifest
        except Exception as exc:
            logger.error("Failed minimal parse of %s: %s", manifest_path, exc)
            return None

    def load_plugin(self, source: str | Path) -> Plugin | None:
        """Load a plugin from a directory path.

        Args:
            source: Path to a directory containing ``plugin.yaml``.

        Returns:
            The loaded Plugin, or None on failure.
        """
        plugin_dir = Path(source).resolve()
        if not plugin_dir.is_dir():
            logger.error("Plugin directory not found: %s", plugin_dir)
            return None

        manifest_path = plugin_dir / "plugin.yaml"
        manifest = self._parse_manifest(manifest_path)
        if manifest is None:
            logger.error("No valid manifest at %s", manifest_path)
            return None

        name: str = str(manifest.get("name", plugin_dir.name))
        kind: str = str(manifest.get("kind", "tool"))
        version: str = str(manifest.get("version", "0.1.0"))
        description: str = str(manifest.get("description", ""))

        # Check for requirements
        requires_env: list[str] = manifest.get("requires_env", []) or []
        for env_var in requires_env:
            if not os.environ.get(env_var):
                logger.warning(
                    "Plugin '%s' requires env var %s (not set)",
                    name,
                    env_var,
                )

        # Create the plugin and context
        ctx = PluginContext(plugin_name=name)
        plugin = Plugin(
            name=name,
            kind=kind,
            version=version,
            description=description,
            manifest_path=str(manifest_path),
            manifest=manifest,
            ctx=ctx,
        )

        # Try to execute plugin init script
        init_script = plugin_dir / "init.py"
        if init_script.exists():
            try:
                import importlib.util

                spec = importlib.util.spec_from_file_location(
                    f"_plugin_{name}", init_script
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    mod.__dict__["ctx"] = ctx  # Inject context
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(ctx)
                    logger.info("Plugin '%s': init.py executed", name)
            except Exception as exc:
                logger.error(
                    "Plugin '%s' init.py failed: %s. Plugin still registered.",
                    name,
                    exc,
                )

        # Register
        self._registry.register(plugin)
        return plugin

    @property
    def _disabled_path(self) -> Path:
        """Durable record of plugins the owner explicitly unloaded."""
        return self._auto_load_path / ".disabled.json"

    def disabled_names(self) -> set[str]:
        """Names of plugins the owner disabled; auto-discovery must skip them."""
        try:
            raw = json.loads(self._disabled_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(raw, list):
            return set()
        return {str(name) for name in raw}

    def _write_disabled(self, names: set[str]) -> None:
        try:
            self._auto_load_path.mkdir(parents=True, exist_ok=True)
            self._disabled_path.write_text(
                json.dumps(sorted(names), indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.error("Failed to persist disabled plugins: %s", exc)

    def disable(self, name: str) -> None:
        """Record *name* as disabled so ``load_all()`` will not resurrect it."""
        self._write_disabled(self.disabled_names() | {name})

    def enable(self, name: str) -> None:
        """Clear the disabled record for *name* (an explicit load re-enables it)."""
        current = self.disabled_names()
        if name in current:
            self._write_disabled(current - {name})

    def discover_plugins(self) -> list[Path]:
        """Discover plugin directories under ``~/.antigona/plugins/``.

        Directories the owner explicitly unloaded are skipped, so listing or
        auto-loading can never re-activate a plugin that was disabled.

        Returns:
            List of absolute paths to discovered plugin directories.
        """
        discovered: list[Path] = []
        if not self._auto_load_path.is_dir():
            return discovered

        disabled = self.disabled_names()
        for entry in self._auto_load_path.iterdir():
            if entry.name in disabled:
                continue
            if entry.is_dir() and (entry / "plugin.yaml").exists():
                discovered.append(entry)

        return sorted(discovered)

    def load_all(self) -> int:
        """Discover and load all plugins from ``~/.antigona/plugins/``.
 
        Returns:
            Number of successfully loaded plugins.
        """
        count = 0
        for plugin_dir in self.discover_plugins():
            plugin = self.load_plugin(plugin_dir)
            if plugin is not None:
                count += 1
        logger.info("Auto-loaded %d plugin(s) from %s", count, self._auto_load_path)
        return count

    def discover_entry_points(self) -> list[Plugin]:
        """Discover plugins registered via pip entry points.

        Requires the ``antigona_plugins`` entry point group.
        """
        plugins: list[Plugin] = []
        try:
            from importlib.metadata import entry_points  # Python 3.9+

            eps = entry_points(group="antigona_plugins")
            for ep in eps:
                try:
                    factory = ep.load()
                    # The entry point should return a Plugin object or a callable(ctx)
                    if callable(factory):
                        result = factory()
                        if isinstance(result, Plugin):
                            self._registry.register(result)
                            plugins.append(result)
                            logger.info(
                                "Loaded entry-point plugin '%s' from %s",
                                ep.name,
                                ep.value,
                            )
                except Exception as exc:
                    logger.error(
                        "Failed to load entry-point plugin '%s': %s",
                        ep.name,
                        exc,
                    )
        except Exception:
            pass
        return plugins
