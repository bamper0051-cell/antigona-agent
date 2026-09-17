"""Text-to-Speech Tool for Antigona.

Provides the 'speech.tts' tool for converting text to audio artifacts.
Verifies file creation, size > 0, and returns artifact metadata.
"""

from __future__ import annotations

import os
from pathlib import Path

from antigona.tools.contracts import (
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolOutput,
    ToolSpec,
)
from antigona.voice.tts import text_to_speech


class TTSTool(Tool):
    """Tool for generating speech audio from text."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="speech.tts",
            category=ToolCategory.MOCK,
            description="Convert text to speech audio file (.ogg/.mp3)",
            risk_level=RiskLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice": {"type": "string"},
                },
                "required": ["text"],
            },
        )

    def validate(self, inp: ToolInput) -> list[str]:
        if not inp.params.get("text"):
            return ["Missing 'text' parameter"]
        return []

    async def execute(self, inp: ToolInput) -> ToolOutput:
        text = inp.params["text"]
        voice = inp.params.get("voice", "alloy")

        audio_path = await text_to_speech(text, voice=voice)
        if not audio_path or not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            return ToolOutput(
                success=False,
                error="TTS generation failed or produced empty audio",
            )

        p = Path(audio_path)
        return ToolOutput(
            success=True,
            data={
                "audio_path": audio_path,
                "size_bytes": p.stat().st_size,
                "format": p.suffix.lstrip("."),
                "voice_marker": f"⟪voice:{audio_path}⟫",
            },
            artifacts=[{"name": p.name, "path": audio_path, "type": "audio"}],
        )
