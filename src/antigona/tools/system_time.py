"""System Date and Time Tool for Antigona.

Provides an authoritative source of current date and time.
Grounds responses so the system never hallucinates dates or times.
"""

from __future__ import annotations

import datetime
from typing import Any

from antigona.tools.contracts import (
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolOutput,
    ToolSpec,
)


def get_current_system_time() -> dict[str, Any]:
    """Get authoritative system date and time."""
    now_utc = datetime.datetime.now(datetime.UTC)
    now_local = datetime.datetime.now()
    return {
        "utc_iso": now_utc.isoformat(),
        "local_iso": now_local.isoformat(),
        "utc_date": now_utc.strftime("%Y-%m-%d"),
        "utc_time": now_utc.strftime("%H:%M:%S"),
        "local_date": now_local.strftime("%Y-%m-%d"),
        "local_time": now_local.strftime("%H:%M:%S"),
        "timestamp": now_utc.timestamp(),
        "day_of_week": now_utc.strftime("%A"),
        "formatted": f"{now_local.strftime('%Y-%m-%d %H:%M:%S')} (UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')})",
    }


def format_system_time_reply() -> str:
    """Render the authoritative date/time as a chat-ready answer.

    Used by the deterministic ``system.time`` routing branch so a date/time
    request is never answered from model memory (hallucinated year).
    """
    data = get_current_system_time()
    return (
        "🕒 Дата и время с сервера:\n"
        f"• локально — {data['local_date']} {data['local_time']}\n"
        f"• UTC — {data['utc_date']} {data['utc_time']}\n"
        f"• день недели — {data['day_of_week']}"
    )


class SystemTimeTool(Tool):
    """Tool to get the current date and time from the host system."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="system.time",
            category=ToolCategory.MOCK,
            description="Get accurate current date and time from the system",
            risk_level=RiskLevel.SAFE,
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "utc_iso": {"type": "string"},
                    "local_iso": {"type": "string"},
                    "formatted": {"type": "string"},
                },
            },
        )

    def validate(self, inp: ToolInput) -> list[str]:
        return []

    async def execute(self, inp: ToolInput) -> ToolOutput:
        data = get_current_system_time()
        return ToolOutput(success=True, data=data)
