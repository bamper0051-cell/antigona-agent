"""Provider abstraction — abstract interface, mock, OpenAI-compatible, registry,
and provider profiles.
"""

from antigona.providers.base import BaseProvider, ProviderError
from antigona.providers.mock import MockProvider
from antigona.providers.openai_compatible import OpenAICompatibleProvider
from antigona.providers.profiles import (
    ProfileRegistry,
    ProviderProfile,
    get_profile_registry,
    register_profile,
)
from antigona.providers.registry import ProviderRegistry, ProviderRegistryError

__all__ = [
    "BaseProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderProfile",
    "ProfileRegistry",
    "ProviderRegistry",
    "ProviderRegistryError",
    "get_profile_registry",
    "register_profile",
]
