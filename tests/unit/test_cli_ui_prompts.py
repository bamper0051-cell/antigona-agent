"""Tests for read_prompt is_password (hidden input) masking in the CLI UI."""

from __future__ import annotations

import pytest

from antigona.cli_ui.prompts import read_prompt


@pytest.mark.anyio
async def test_read_prompt_passes_is_password_true_to_prompt_async():
    """Password input must be masked (is_password=True forwarded to prompt_async)."""
    calls = {}

    class FakeSession:
        async def prompt_async(self, prompt_str, **kwargs):
            calls["kwargs"] = kwargs
            return "537899"

    raw = await read_prompt(prompt_str="PIN:", session=FakeSession(), is_password=True)
    assert raw == "537899"
    assert calls["kwargs"].get("is_password") is True


@pytest.mark.anyio
async def test_read_prompt_normal_input_has_no_is_password():
    """Plain (non-password) input must NOT be masked."""
    calls = {}

    class FakeSession:
        async def prompt_async(self, prompt_str, **kwargs):
            calls["kwargs"] = kwargs
            return "hello"

    raw = await read_prompt(prompt_str="> ", session=FakeSession())
    assert raw == "hello"
    assert "is_password" not in calls["kwargs"] or calls["kwargs"].get("is_password") is False
