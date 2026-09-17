from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field

InputT = TypeVar("InputT", bound=BaseModel, contravariant=True)


class WriteFileInput(BaseModel):
    path: str = Field(min_length=1)
    content: str


@dataclass(frozen=True)
class Evidence:
    kind: str
    value: str


@dataclass(frozen=True)
class ArtifactResult:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    status: Literal["completed", "failed", "cancelled"]
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    artifacts: list[ArtifactResult] = field(default_factory=list)
    retryable: bool = False


class Tool(Protocol, Generic[InputT]):
    name: str
    description: str
    risk_level: Literal["low", "medium", "high"]
    timeout_seconds: int
    requires_approval: bool
    sandbox_required: bool

    def execute(self, arguments: InputT) -> ToolResult: ...
