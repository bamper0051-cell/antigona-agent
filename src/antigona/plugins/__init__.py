"""Plugin system — dynamic extensibility for Antigona.

Plugins are loadable packages that register tools, hooks, commands, and skills
at runtime. Each plugin ships a ``plugin.yaml`` manifest and is managed by
the **PluginRegistry**.

Typical usage::

    from antigona.plugins import PluginRegistry, PluginLoader

    registry = PluginRegistry()
    loader = PluginLoader(registry)
    loader.discover_plugins()
    loader.load_plugin("my-plugin")
    registry.list()
"""

from antigona.plugins.plugin import (
    Plugin,
    PluginContext,
    PluginLlmAccess,
    PluginLoader,
    PluginRegistry,
)

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginLlmAccess",
    "PluginLoader",
    "PluginRegistry",
]
