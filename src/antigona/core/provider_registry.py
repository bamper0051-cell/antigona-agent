from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderRegistryError(RuntimeError):
    pass


class UnknownProviderRole(ProviderRegistryError, ValueError):
    pass


class ProviderRole(StrEnum):
    EXECUTOR = "executor"
    REVIEWER_ARCH = "reviewer_arch"
    REVIEWER_SEC = "reviewer_sec"
    REVIEWER_OPS = "reviewer_ops"
    TRIAGE = "triage"


class CostClass(StrEnum):
    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    model: str
    version: str
    roles: frozenset[ProviderRole]
    languages: tuple[str, ...]
    max_context_tokens: int
    supports_terminal: bool
    supports_tests: bool
    supports_patch: bool
    supports_long_review: bool
    cost_class: CostClass
    available: bool
    known_limitations: tuple[str, ...]


def _role_from_value(role: ProviderRole | str) -> ProviderRole:
    if isinstance(role, ProviderRole):
        return role
    try:
        return ProviderRole(role)
    except ValueError as exc:
        raise UnknownProviderRole(role) from exc


def _cost_sort_key(cost: CostClass) -> int:
    if cost is CostClass.LOW:
        return 0
    if cost is CostClass.MED:
        return 1
    return 2


class ProviderRegistry:
    def __init__(self) -> None:
        self._caps: dict[tuple[str, str, str], ProviderCapability] = {}
        for capability in _seed_capabilities():
            self.register(capability)

    def register(self, cap: ProviderCapability) -> None:
        key = (cap.provider, cap.model, cap.version)
        self._caps[key] = cap

    def get(self, provider: str, model: str) -> ProviderCapability | None:
        matches = [cap for cap in self._caps.values() if cap.provider == provider and cap.model == model]
        if not matches:
            return None
        return sorted(matches, key=lambda cap: cap.version)[-1]

    def find(
        self,
        role: ProviderRole | str,
        *,
        min_context: int = 0,
        cost_class: CostClass | None = None,
        available_only: bool = True,
    ) -> tuple[ProviderCapability, ...]:
        expected_role = _role_from_value(role)
        filtered = [
            capability
            for capability in self._caps.values()
            if expected_role in capability.roles
            and capability.max_context_tokens >= min_context
            and (cost_class is None or capability.cost_class is cost_class)
            and (not available_only or capability.available)
        ]
        ordered = sorted(
            filtered,
            key=lambda capability: (
                _cost_sort_key(capability.cost_class),
                capability.provider,
                capability.model,
                capability.version,
            ),
        )
        return tuple(ordered)

    def all(self) -> tuple[ProviderCapability, ...]:
        ordered = sorted(
            self._caps.values(),
            key=lambda capability: (
                _cost_sort_key(capability.cost_class),
                capability.provider,
                capability.model,
                capability.version,
            ),
        )
        return tuple(ordered)


def _seed_capabilities() -> tuple[ProviderCapability, ...]:
    return (
        ProviderCapability(
            provider="codex",
            model="gpt-5.3-codex",
            version="5.3",
            roles=frozenset({ProviderRole.EXECUTOR, ProviderRole.REVIEWER_ARCH, ProviderRole.REVIEWER_SEC}),
            languages=("ru", "en", "python"),
            max_context_tokens=256_000,
            supports_terminal=True,
            supports_tests=True,
            supports_patch=True,
            supports_long_review=True,
            cost_class=CostClass.MED,
            available=True,
            known_limitations=(),
        ),
        ProviderCapability(
            provider="agy",
            model="Antigravity",
            version="current",
            roles=frozenset({ProviderRole.EXECUTOR, ProviderRole.REVIEWER_ARCH, ProviderRole.REVIEWER_SEC}),
            languages=("ru", "en", "python"),
            max_context_tokens=200_000,
            supports_terminal=True,
            supports_tests=True,
            supports_patch=True,
            supports_long_review=True,
            cost_class=CostClass.MED,
            available=True,
            known_limitations=(),
        ),
        ProviderCapability(
            provider="claude",
            model="Claude Code",
            version="current",
            roles=frozenset({ProviderRole.EXECUTOR, ProviderRole.REVIEWER_ARCH}),
            languages=("ru", "en", "python"),
            max_context_tokens=200_000,
            supports_terminal=True,
            supports_tests=True,
            supports_patch=True,
            supports_long_review=True,
            cost_class=CostClass.HIGH,
            available=False,
            known_limitations=("monthly spend limit",),
        ),
        ProviderCapability(
            provider="blackbox",
            model="blackbox-sec",
            version="current",
            roles=frozenset({ProviderRole.REVIEWER_SEC}),
            languages=("ru", "en", "python"),
            max_context_tokens=128_000,
            supports_terminal=False,
            supports_tests=False,
            supports_patch=False,
            supports_long_review=True,
            cost_class=CostClass.HIGH,
            available=True,
            known_limitations=("catalog-only security reviewer",),
        ),
        ProviderCapability(
            provider="hermes-local",
            model="deepseek-v4-flash",
            version="v4-flash",
            roles=frozenset({ProviderRole.REVIEWER_OPS, ProviderRole.TRIAGE}),
            languages=("ru", "en", "python"),
            max_context_tokens=64_000,
            supports_terminal=False,
            supports_tests=False,
            supports_patch=False,
            supports_long_review=False,
            cost_class=CostClass.LOW,
            available=True,
            known_limitations=("local-ops profile",),
        ),
    )

