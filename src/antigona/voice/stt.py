"""Speech-to-Text — transcribe audio files to text.

Uses ``faster-whisper`` (local) or OpenAI Whisper API as fallback.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


async def speech_to_text(audio_path: str, language: str | None = None) -> str:
    """Transcribe an audio file to text.

    Priority:
    1. ``faster-whisper`` (local, free) — always tried first.
    2. OpenAI Whisper API (requires ``OPENAI_API_KEY``).

    Args:
        audio_path: Path to an audio file (ogg, mp3, wav, m4a, etc.).
        language: Optional language code (``"ru"``, ``"en"``, etc.).
                  ``None`` = auto-detect.

    Returns:
        Transcribed text, or empty string on failure.
    """
    # Try faster-whisper first (local, free)
    text = await _faster_whisper_stt(audio_path, language=language)
    if text:
        return text

    # Fallback to OpenAI Whisper API
    text = await _openai_stt(audio_path, language=language)
    if text:
        return text

    return ""


async def _faster_whisper_stt(audio_path: str, language: str | None = None) -> str:
    """Transcribe via faster-whisper (local free model)."""
    try:
        from faster_whisper import WhisperModel

        # Use small model for speed; medium for accuracy
        model_size = os.getenv("WHISPER_MODEL", "small")
        logger.info("faster-whisper: loading model '%s'...", model_size)

        # Run CPU inference in executor to avoid blocking
        import asyncio

        loop = asyncio.get_running_loop()

        def _transcribe() -> str:
            model = WhisperModel(
                model_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=4,
                num_workers=2,
            )
            segments, info = model.transcribe(
                audio_path,
                language=language,
                beam_size=5,
                vad_filter=True,
            )
            text_parts = []
            for seg in segments:
                text_parts.append(seg.text.strip())
            return " ".join(text_parts)

        result = await loop.run_in_executor(None, _transcribe)
        if result.strip():
            logger.info("faster-whisper: transcribed %d chars", len(result))
            return result.strip()
    except ImportError:
        logger.warning("faster-whisper not installed, skipping local STT")
    except Exception as exc:
        logger.warning("faster-whisper STT failed: %s", exc)

    return ""


async def _openai_stt(audio_path: str, language: str | None = None) -> str:
    """Transcribe via OpenAI Whisper API."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key in ("", "dummy-key", "dummy"):
        return ""

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

        with open(audio_path, "rb") as f:
            kwargs: dict[str, Any] = {
                "model": "whisper-1",
                "file": f,
                "response_format": "text",
            }
            if language:
                kwargs["language"] = language

            transcript = await client.audio.transcriptions.create(**kwargs)

        text = transcript.strip() if isinstance(transcript, str) else transcript.text.strip()
        if text:
            logger.info("OpenAI STT: transcribed %d chars", len(text))
            return text
    except Exception as exc:
        logger.warning("OpenAI STT failed: %s", exc)

    return ""
