from __future__ import annotations

import inspect

import pytest

from antigona.core.provider_registry import (
    CostClass,
    ProviderCapability,
    ProviderRegistry,
    ProviderRole,
    UnknownProviderRole,
)


def test_register_get_and_idempotency() -> None:
    registry = ProviderRegistry()
    base_count = len(registry.all())
    capability = ProviderCapability(
        provider="test-provider",
        model="test-model",
        version="1",
        roles=frozenset({ProviderRole.EXECUTOR}),
        languages=("en",),
        max_context_tokens=8_192,
        supports_terminal=False,
        supports_tests=False,
        supports_patch=False,
        supports_long_review=False,
        cost_class=CostClass.LOW,
        available=True,
        known_limitations=(),
    )

    registry.register(capability)
    registry.register(capability)

    assert len(registry.all()) == base_count + 1
    assert registry.get("test-provider", "test-model") == capability
    assert registry.get("missing", "missing") is None


def test_find_is_deterministic_and_filters_availability() -> None:
    registry = ProviderRegistry()

    executors_default = registry.find(ProviderRole.EXECUTOR)
    executors_with_unavailable = registry.find(ProviderRole.EXECUTOR, available_only=False)

    assert [cap.provider for cap in executors_default] == ["agy", "codex"]
    assert [cap.provider for cap in executors_with_unavailable] == ["agy", "codex", "claude"]
    assert executors_default == registry.find(ProviderRole.EXECUTOR)


def test_find_filters_by_context_and_cost() -> None:
    registry = ProviderRegistry()

    triage_low = registry.find(ProviderRole.TRIAGE, min_context=1, cost_class=CostClass.LOW)
    triage_high = registry.find(ProviderRole.TRIAGE, cost_class=CostClass.HIGH)

    assert [cap.provider for cap in triage_low] == ["hermes-local"]
    assert triage_high == ()


def test_find_unknown_role_is_fail_closed() -> None:
    registry = ProviderRegistry()

    with pytest.raises(UnknownProviderRole):
        registry.find("unknown-role")


def test_registry_has_catalog_only_surface() -> None:
    method_names = {name for name, _ in inspect.getmembers(ProviderRegistry, inspect.isfunction)}

    assert {"register", "get", "find", "all"}.issubset(method_names)
    assert "execute" not in method_names
    assert "run" not in method_names
    assert "dispatch" not in method_names

    for method_name in ("register", "get", "find", "all"):
        signature = inspect.signature(getattr(ProviderRegistry, method_name))
        assert "task" not in signature.parameters
        assert "goal" not in signature.parameters

