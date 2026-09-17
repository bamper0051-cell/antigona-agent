"""ImageGenerator — generate images via Pollinations.ai free API.

Usage:
    generator = ImageGenerator()
    path = await generator.generate_and_send(
        prompt="море, закат, волны",
        message=telegram_message,
    )
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from antigona.core import paths

logger = logging.getLogger(__name__)

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
def _default_output_dir() -> Path:
    """Generated images are workspace artifacts, never code-root state."""
    return paths.workspace_dir() / "output"


class ImageGenerator:
    """Generate images from text prompts using the free Pollinations.ai API.

    Attributes:
        output_dir: Directory where generated images are saved.
        base_url: Pollinations API base URL.
    """

    def __init__(
        self,
        output_dir: str | Path | None = None,
        base_url: str = POLLINATIONS_BASE,
    ) -> None:
        self.output_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url

    async def generate(
        self,
        prompt: str,
        width: int = 1920,
        height: int = 1080,
    ) -> bytes:
        """Download a generated image from Pollinations.ai.

        Args:
            prompt: Text description of the image (in any language).
            width: Image width in pixels (default 1920).
            height: Image height in pixels (default 1080).

        Returns:
            Raw image bytes.

        Raises:
            RuntimeError: If the HTTP request fails or returns non-200.
        """
        import httpx

        url = f"{self.base_url}/{prompt}?width={width}&height={height}"
        logger.info("Fetching image from Pollinations: %s", url)

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(url)

        if response.status_code != 200:
            raise RuntimeError(
                f"Pollinations API returned {response.status_code}: "
                f"{response.text[:200]}"
            )

        return response.content

    def save_image(self, image_data: bytes, path: str | Path) -> Path:
        """Save raw image bytes to a file.

        Args:
            image_data: Raw image bytes.
            path: Destination file path.

        Returns:
            The resolved Path that was written to.
        """
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(image_data)
        logger.info("Image saved: %s (%d bytes)", dest, len(image_data))
        return dest

    async def generate_and_send(
        self,
        prompt: str,
        message: Any | None = None,
        width: int = 1920,
        height: int = 1080,
    ) -> str:
        """Generate an image and optionally send it to Telegram.

        Args:
            prompt: Text description of the image.
            message: Optional Telegram Message object. If provided, the image
                     is sent via message.answer_document().
            width: Image width in pixels.
            height: Image height in pixels.

        Returns:
            The path to the saved image file as a string.

        Raises:
            RuntimeError: If generation fails or Telegram send fails.
        """
        # Sanitize prompt for filename
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)
        safe_name = safe_name.strip().replace(" ", "_")[:60]
        ts = int(time.time())
        filename = f"pollinations_{safe_name}_{ts}.jpg"
        filepath = self.output_dir / filename

        # Generate
        image_data = await self.generate(prompt, width=width, height=height)
        self.save_image(image_data, filepath)

        # Send via Telegram if message object is provided
        if message is not None:
            try:
                from aiogram.types import FSInputFile

                await message.answer_document(
                    document=FSInputFile(str(filepath)),
                    caption=f"🖼 {prompt[:100]}",
                )
            except Exception as e:
                logger.warning("Failed to send image via Telegram: %s", e)
                raise RuntimeError(
                    f"Не удалось отправить изображение: {e}"
                ) from e

        return str(filepath)
