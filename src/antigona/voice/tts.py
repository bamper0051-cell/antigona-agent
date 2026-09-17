"""Text-to-Speech — convert text to Ogg Opus for Telegram voice messages.

Uses OpenAI TTS API (or compatible) via the OpenAI Python SDK.
Falls back to ``edge-tts`` if ``OPENAI_API_KEY`` is not set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from antigona.core import paths

logger = logging.getLogger(__name__)


async def text_to_speech(text: str, voice: str = "alloy") -> str | None:
    """Convert *text* to speech and return the path to an Ogg Opus file.

    Args:
        text: The text to speak.
        voice: TTS voice name (``alloy``, ``echo``, ``fable``, ``onyx``,
               ``nova``, ``shimmer``). Default ``alloy``.

    Returns:
        Absolute path to the Ogg file, or ``None`` on failure.
    """
    # Truncate very long text
    if len(text) > 4000:
        text = text[:3997] + "..."

    # Ensure output dir
    out_dir = str(paths.voice_cache_dir())
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"tts_{uuid.uuid4().hex[:12]}.ogg")

    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key and api_key not in ("", "dummy-key", "dummy"):
        return await _openai_tts(text, voice, out_path)
    else:
        return await _edge_tts_fallback(text, out_path)


async def _openai_tts(text: str, voice: str, out_path: str) -> str | None:
    """TTS via OpenAI (or compatible) API."""
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        response = await client.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text,
            response_format="opus",
        )
        with open(out_path, "wb") as f:
            f.write(response.content)
        logger.info("OpenAI TTS: %d chars -> %s", len(text), out_path)
        return out_path
    except Exception as exc:
        logger.warning("OpenAI TTS failed: %s. Falling back to edge-tts.", exc)
        return await _edge_tts_fallback(text, out_path)


async def _edge_tts_fallback(text: str, out_path: str) -> str | None:
    """TTS via local edge-tts (free, no API key needed)."""
    if _check_ffmpeg() and _check_edge_tts():
        # Use subprocess with edge-tts

        cmd = [
            "edge-tts",
            "--text", text,
            "--voice", "ru-RU-SvetlanaNeural",  # Russian-friendly default
            "--write-media", out_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                logger.info("edge-tts: %d chars -> %s", len(text), out_path)
                return out_path
            else:
                logger.warning("edge-tts failed: %s", stderr.decode()[:200])
        except Exception as exc:
            logger.warning("edge-tts error: %s", exc)

    # Last resort: try gTTS
    return await _gtts_fallback(text, out_path)


async def _gtts_fallback(text: str, out_path: str) -> str | None:
    """TTS via gTTS (Google, no API key)."""
    try:
        from gtts import gTTS

        tts = gTTS(text=text, lang="ru", slow=False)
        # gTTS saves as mp3 — convert to ogg if ffmpeg available
        mp3_path = out_path.replace(".ogg", ".mp3")
        tts.save(mp3_path)
        if _check_ffmpeg():
            import subprocess

            subprocess.run(
                ["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus", "-b:a", "24k", out_path],
                capture_output=True, timeout=30,
            )
            os.remove(mp3_path)
            logger.info("gTTS+ffmpeg: %d chars -> %s", len(text), out_path)
            return out_path
        else:
            # Return mp3 as-is (Telegram accepts mp3 voice)
            os.rename(mp3_path, out_path)
            logger.info("gTTS (mp3): %d chars -> %s", len(text), out_path)
            return out_path
    except Exception as exc:
        logger.warning("gTTS fallback also failed: %s", exc)
        return None


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is installed."""
    import shutil
    return shutil.which("ffmpeg") is not None


def _check_edge_tts() -> bool:
    """Check if edge-tts CLI is installed."""
    import shutil
    return shutil.which("edge-tts") is not None
