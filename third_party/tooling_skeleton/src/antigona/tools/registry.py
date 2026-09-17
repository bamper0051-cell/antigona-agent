from __future__ import annotations

import threading
from typing import Iterable

from .contracts import ToolContext, ToolSpec


class ToolRegistry:
    """Thread-safe central registry. One tool name has exactly one owner."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._generation = 0
        self._lock = threading.RLock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def register(self, spec: ToolSpec, *, replace: bool = False) -> None:
        with self._lock:
            if spec.name in self._tools and not replace:
                raise ValueError(f"Tool already registered: {spec.name}")
            self._tools[spec.name] = spec
            self._generation += 1

    def deregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)
            self._generation += 1

    def get(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._tools.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"Unknown tool: {name}")
        return spec

    def snapshot(self) -> tuple[ToolSpec, ...]:
        with self._lock:
            return tuple(self._tools.values())

    def available(self, context: ToolContext) -> tuple[ToolSpec, ...]:
        result: list[ToolSpec] = []
        for spec in self.snapshot():
            check = spec.availability_check
            try:
                if check is None or check(context):
                    result.append(spec)
            except Exception:
                continue
        return tuple(result)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(spec.name for spec in self.snapshot()))

    def register_many(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)
