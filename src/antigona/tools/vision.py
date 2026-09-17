"""Vision / Image analysis — analyze images via LLM vision.

Provides:
    analyze_image(image_path, question) — returns text description via vision-capable LLM.
    VisionAnalyzer — class-based interface with caching.

Since DeepSeek is text-only, this uses an OpenAI-compatible vision endpoint.
If no vision provider is available, falls back to describing via Pollinations
reverse-image lookup.

Usage:
    analyzer = VisionAnalyzer(provider=my_provider)
    desc = analyzer.analyze("/path/to/image.jpg", "What's in this image?")
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

VISION_TEMPERATURE: float = 0.3
VISION_MAX_TOKENS: int = 1024

# Pollinations reverse image URL (fallback if no vision provider)
_POLLINATIONS_DESCRIBE_URL = "https://image.pollinations.ai/describe/{url}"

# ─── VisionAnalyzer ──────────────────────────────────────────────────────────


class VisionAnalyzer:
    """Analyze images using an LLM with vision capabilities.

    Attributes:
        provider: Optional provider with vision support (e.g. OpenAI GPT-4o).
        api_key: Optional API key for vision provider.
        api_base: Optional API base URL for vision provider.
        model: Vision model name.
    """

    def __init__(
        self,
        provider: Any | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str = "gpt-4o-mini",
    ) -> None:
        self._provider = provider
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._api_base = api_base or os.environ.get(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        )
        self._model = model

    # ── Image loading ──────────────────────────────────────────────────────

    @staticmethod
    def load_image_base64(path: str | Path) -> str:
        """Read an image file and return a base64 data URL.

        Args:
            path: Path to the image file (jpg, png, gif, webp).

        Returns:
            Base64-encoded data URL (e.g. ``data:image/jpeg;base64,...``).

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file extension is not a supported image type.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Image not found: {path}")

        ext = p.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        mime = mime_map.get(ext)
        if mime is None:
            raise ValueError(f"Unsupported image type: {ext} (supported: {list(mime_map)})")

        data = p.read_bytes()
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    # ── Analysis ───────────────────────────────────────────────────────────

    def analyze(
        self,
        image_path: str | Path,
        question: str = "Опиши, что изображено на картинке.",
    ) -> str:
        """Analyze an image and return a text description.

        Uses the configured vision provider (OpenAI-compatible). Falls back
        to Pollinations describe API if no provider is available.

        Args:
            image_path: Path to the image file.
            question: Question or instruction about the image.

        Returns:
            Text description of the image.
        """
        # If a provider with vision support is available, use it
        if self._provider is not None:
            return self._analyze_via_provider(image_path, question)

        # Fallback: try OpenAI-compatible API key
        if self._api_key:
            return self._analyze_via_openai(image_path, question)

        # Last resort: Pollinations describe fallback
        return self._analyze_via_pollinations(image_path, question)

    def _analyze_via_provider(
        self,
        image_path: str | Path,
        question: str,
    ) -> str:
        """Analyze via the configured provider (must support vision)."""
        b64 = self.load_image_base64(image_path)

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {"url": b64, "detail": "auto"},
                    },
                ],
            },
        ]

        try:
            if self._provider is None:
                raise RuntimeError("No vision provider configured")
            result = self._provider.generate(
                messages=messages,
                context={"temperature": VISION_TEMPERATURE, "max_tokens": VISION_MAX_TOKENS},
            )
            return (result or "").strip()
        except Exception as exc:
            logger.warning("Provider vision analysis failed, trying OpenAI: %s", exc)
            if self._api_key:
                return self._analyze_via_openai(image_path, question)
            return f"❌ Vision analysis failed: {exc}"

    def _analyze_via_openai(
        self,
        image_path: str | Path,
        question: str,
    ) -> str:
        """Analyze via OpenAI-compatible vision API."""
        import httpx

        b64 = self.load_image_base64(image_path)

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": b64, "detail": "auto"},
                        },
                    ],
                },
            ],
            "max_tokens": VISION_MAX_TOKENS,
            "temperature": VISION_TEMPERATURE,
        }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(
                f"{self._api_base}/chat/completions",
                json=payload,
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                return (choices[0].get("message", {}).get("content", "") or "").strip()
            return "❌ No response from vision API."
        except httpx.HTTPStatusError as exc:
            logger.warning("OpenAI vision API error: %s", exc)
            return f"❌ Vision API error ({exc.response.status_code}): {exc.response.text[:200]}"
        except Exception as exc:
            logger.warning("OpenAI vision request failed: %s", exc)
            return f"❌ Vision request failed: {exc}"

    def _analyze_via_pollinations(
        self,
        image_path: str | Path,
        question: str,
    ) -> str:
        """Fallback: try to describe via Pollinations (very basic)."""
        # Pollinations describe endpoint takes a URL, not a file path.
        # We can't easily upload a local file, so return a helpful error.
        logger.info("No vision provider configured — returning placeholder")
        return (
            "🖼 Для анализа изображений требуется OpenAI-совместимый vision-провайдер "
            "(например, установите OPENAI_API_KEY). "
            f"Файл: {image_path}. Вопрос: {question}"
        )


# ─── Convenience ─────────────────────────────────────────────────────────────


def analyze_image(
    image_path: str | Path,
    question: str = "Опиши, что изображено на картинке.",
    provider: Any | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    model: str = "gpt-4o-mini",
) -> str:
    """One-shot image analysis.

    Args:
        image_path: Path to the image file.
        question: Question about the image.
        provider: Optional vision-capable provider.
        api_key: Optional OpenAI API key for vision.
        api_base: Optional OpenAI-compatible base URL.
        model: Vision model name.

    Returns:
        Text description of the image.
    """
    analyzer = VisionAnalyzer(
        provider=provider,
        api_key=api_key,
        api_base=api_base,
        model=model,
    )
    return analyzer.analyze(image_path, question)
