from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolEvent:
    event_type: str
    operation_id: str
    call_id: str
    tool_name: str
    payload: Mapping[str, Any] = field(default_factory=dict)
