"""Tests for ImageGenerator — Pollinations.ai image generation.

Covers:
  - ImageGenerator.generate() with mock HTTP
  - ImageGenerator.save_image()
  - ImageGenerator.generate_and_send() with mock message
  - ActionExecutor GENERATE_IMAGE parsing and execution
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from antigona.tools.action_executor import Action, ActionExecutor, ActionType
from antigona.tools.image_gen import ImageGenerator


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execution-mechanics tests must not depend on ambient ANTIGONA_PIN."""
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)


class TestImageGeneratorGenerate:
    """Tests for ImageGenerator.generate() — mock HTTP."""

    @pytest.mark.asyncio
    async def test_generate_success(self) -> None:
        """Test that generate() downloads image bytes successfully."""
        mock_image_data = b"fake_image_bytes"

        with tempfile.TemporaryDirectory() as tmpdir:
            generator = ImageGenerator(output_dir=tmpdir)

            # Mock httpx.AsyncClient.get
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = mock_image_data

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.get.return_value = mock_response

                result = await generator.generate("test prompt", width=512, height=512)

                assert result == mock_image_data
                mock_client.get.assert_called_once()
                call_url = mock_client.get.call_args[0][0]
                assert "test prompt" in call_url
                assert "width=512" in call_url
                assert "height=512" in call_url

    @pytest.mark.asyncio
    async def test_generate_http_error(self) -> None:
        """Test that generate() raises on non-200 response."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generator = ImageGenerator(output_dir=tmpdir)

            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.text = "Internal Server Error"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.get.return_value = mock_response

                with pytest.raises(RuntimeError, match="500"):
                    await generator.generate("test")

    @pytest.mark.asyncio
    async def test_generate_custom_size(self) -> None:
        """Test that custom width/height are passed in the URL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generator = ImageGenerator(output_dir=tmpdir)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = b"data"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.get.return_value = mock_response

                await generator.generate("море", width=800, height=600)

                call_url = mock_client.get.call_args[0][0]
                assert "width=800" in call_url
                assert "height=600" in call_url


class TestImageGeneratorSave:
    """Tests for ImageGenerator.save_image()."""

    def test_save_image_success(self) -> None:
        """Test that save_image() writes bytes to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "test.jpg"
            generator = ImageGenerator(output_dir=tmpdir)
            result = generator.save_image(b"image_bytes", str(dest))

            assert result == dest
            assert dest.exists()
            assert dest.read_bytes() == b"image_bytes"

    def test_save_image_creates_parents(self) -> None:
        """Test that save_image() creates parent directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "subdir" / "nested" / "test.jpg"
            generator = ImageGenerator(output_dir=tmpdir)
            result = generator.save_image(b"data", str(dest))

            assert result == dest
            assert dest.exists()
            assert dest.parent.exists()


class TestImageGeneratorGenerateAndSend:
    """Tests for ImageGenerator.generate_and_send()."""

    @pytest.mark.asyncio
    async def test_generate_and_send_no_message(self) -> None:
        """Test generate_and_send without Telegram message."""
        mock_image_data = b"fake_image_bytes"

        with tempfile.TemporaryDirectory() as tmpdir:
            generator = ImageGenerator(output_dir=tmpdir)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = mock_image_data

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.get.return_value = mock_response

                filepath = await generator.generate_and_send(
                    "test prompt", message=None
                )
                assert Path(filepath).exists()
                assert Path(filepath).read_bytes() == mock_image_data

    @pytest.mark.asyncio
    async def test_generate_and_send_with_message(self) -> None:
        """Test generate_and_send with a mock Telegram message."""
        mock_image_data = b"fake_image_bytes"

        with tempfile.TemporaryDirectory() as tmpdir:
            generator = ImageGenerator(output_dir=tmpdir)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = mock_image_data

            mock_message = MagicMock()
            mock_message.answer_document = AsyncMock()

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.get.return_value = mock_response

                filepath = await generator.generate_and_send(
                    "test prompt", message=mock_message
                )

                assert Path(filepath).exists()
                mock_message.answer_document.assert_called_once()


class TestActionExecutorGenerateImage:
    """Tests for GENERATE_IMAGE in ActionExecutor."""

    def test_parse_generate_image(self) -> None:
        """Test that GENERATE_IMAGE|prompt is parsed correctly."""
        executor = ActionExecutor()
        text = "GENERATE_IMAGE|море, закат, волны"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].type == ActionType.GENERATE_IMAGE
        assert actions[0].content == "море, закат, волны"

    def test_parse_generate_image_case_insensitive(self) -> None:
        """Test that GENERATE_IMAGE is case-insensitive."""
        executor = ActionExecutor()
        text = "generate_image|горы"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].type == ActionType.GENERATE_IMAGE
        assert actions[0].content == "горы"

    def test_parse_generate_image_multiline(self) -> None:
        """Test GENERATE_IMAGE with multi-word prompt."""
        executor = ActionExecutor()
        text = "GENERATE_IMAGE|красивое море на закате, волны"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].type == ActionType.GENERATE_IMAGE
        assert "красивое море" in actions[0].content

    def test_strip_generate_image(self) -> None:
        """Test that strip_action_commands removes GENERATE_IMAGE."""
        executor = ActionExecutor()
        text = "GENERATE_IMAGE|море\nГотово!"
        cleaned = executor.strip_action_commands(text)
        assert "GENERATE_IMAGE" not in cleaned
        assert "Готово!" in cleaned

    def test_parse_multiple_commands_including_image(self) -> None:
        """Test parsing mixed commands including GENERATE_IMAGE.

        Note: regex patterns are applied in order (WRITE, SEND, SHELL,
        CONFIGURE_KEY, IMAGE), so GENERATE_IMAGE may not appear first in output.
        """
        executor = ActionExecutor()
        text = (
            "GENERATE_IMAGE|море\n"
            "SEND_FILE|/tmp/report.pdf\n"
            "RUN_SHELL|echo done"
        )
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 3
        # Check all types are present regardless of order
        types = {a.type for a in actions}
        assert ActionType.GENERATE_IMAGE in types
        assert ActionType.SEND_FILE in types
        assert ActionType.RUN_SHELL in types

    @pytest.mark.asyncio
    async def test_execute_generate_image_success(self) -> None:
        """Test that _execute_generate_image calls ImageGenerator."""
        executor = ActionExecutor()

        with tempfile.TemporaryDirectory() as tmpdir:
            action = Action(
                type=ActionType.GENERATE_IMAGE,
                content="test prompt",
            )

            mock_filepath = str(Path(tmpdir) / "test.jpg")

            # Patch ImageGenerator at the module where it's imported
            with patch(
                "antigona.tools.image_gen.ImageGenerator"
            ) as mock_gen_cls:
                mock_gen = MagicMock()
                mock_gen_cls.return_value = mock_gen
                mock_gen.generate_and_send = AsyncMock(return_value=mock_filepath)

                result = await executor.execute(action)

                assert result.success is True
                assert result.action_type == ActionType.GENERATE_IMAGE
                assert result.path == mock_filepath

    @pytest.mark.asyncio
    async def test_execute_generate_image_empty_prompt(self) -> None:
        """Test that empty content returns failure."""
        executor = ActionExecutor()
        action = Action(
            type=ActionType.GENERATE_IMAGE,
            content="",
        )
        result = await executor.execute(action)
        assert result.success is False
        assert "пустой" in result.message

    @pytest.mark.asyncio
    async def test_execute_generate_image_with_message(self) -> None:
        """Test execution with a Telegram message object."""
        executor = ActionExecutor()

        with tempfile.TemporaryDirectory() as tmpdir:
            action = Action(
                type=ActionType.GENERATE_IMAGE,
                content="test prompt",
            )

            mock_message = MagicMock()

            # Patch ImageGenerator at the module where it's imported
            with patch(
                "antigona.tools.image_gen.ImageGenerator"
            ) as mock_gen_cls:
                mock_gen = MagicMock()
                mock_gen_cls.return_value = mock_gen
                mock_gen.generate_and_send = AsyncMock(
                    return_value=str(Path(tmpdir) / "out.jpg")
                )

                result = await executor.execute(action, message=mock_message)

                assert result.success is True
                mock_gen.generate_and_send.assert_called_once_with(
                    prompt="test prompt",
                    message=mock_message,
                )

    @pytest.mark.asyncio
    async def test_execute_generate_image_http_error(self) -> None:
        """Test that HTTP errors return failure ActionResult."""
        executor = ActionExecutor()
        action = Action(
            type=ActionType.GENERATE_IMAGE,
            content="test prompt",
        )

        with patch(
            "antigona.tools.image_gen.ImageGenerator"
        ) as mock_gen_cls:
            mock_gen = MagicMock()
            mock_gen_cls.return_value = mock_gen
            mock_gen.generate_and_send = AsyncMock(
                side_effect=RuntimeError("API returned 500")
            )

            result = await executor.execute(action)

            assert result.success is False
            assert "500" in result.error
