"""Tests for CLI chat command — token resolution, async startup, no RuntimeWarning."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import httpx
import pytest
import typer

import antigona.cli as cli
from antigona.core import paths


def _antigona_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run antigona.cli as a subprocess and return the result."""
    cmd = [
        sys.executable,
        "-m",
        "antigona.cli",
        *args,
    ]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        input="/quit\n",
        capture_output=True,
        text=True,
        timeout=15,
        env=full_env,
        cwd=str(paths.project_root()),
    )


class TestChatTokenResolution:
    """Token resolution: --token arg, env var, .env fallback."""

    def test_chat_with_token_arg(self) -> None:
        """--token argument is passed correctly."""
        result = _antigona_cli("chat", "--token", "test-token")
        assert "Error: Gateway token is required" not in (result.stderr + result.stdout)

    def test_chat_with_env_var(self) -> None:
        """ANTIGONA_GATEWAY_TOKEN env var is read correctly."""
        env = {"ANTIGONA_GATEWAY_TOKEN": "env-token", "PYTHONPATH": "src"}
        result = _antigona_cli("chat", env=env)
        assert "Error: Gateway token is required" not in (result.stderr + result.stdout)

    def test_chat_without_token_uses_env_fallback(self, tmp_path) -> None:
        """No --token and no env var — falls back to .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text("ANTIGONA_GATEWAY_TOKEN=fallback-env-token\n", encoding="utf-8")
        env = os.environ.copy()
        env.pop("ANTIGONA_GATEWAY_TOKEN", None)
        env["ANTIGONA_PROJECT_ROOT"] = str(tmp_path)
        env["PYTHONPATH"] = str((paths.project_root() / "src").resolve())
        result = subprocess.run(
            [sys.executable, "-m", "antigona.cli", "chat"],
            input="/quit\n",
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            cwd=str(tmp_path),
        )
        assert "Error: Gateway token is required" not in (result.stderr + result.stdout)

    @pytest.mark.skipif(sys.platform == "win32", reason='chat subprocess needs a Windows console (NoConsoleScreenBufferError) (Wave 4)')
    def test_chat_no_token_fails_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No token anywhere — clear error message."""
        import tempfile

        monkeypatch.delenv("ANTIGONA_GATEWAY_TOKEN", raising=False)
        monkeypatch.delenv("ANTIGONA_PIN", raising=False)
        monkeypatch.delenv("ANTIGONA_DEV_TOKENS", raising=False)

        tmpdir = tempfile.mkdtemp()
        env = os.environ.copy()
        env.pop("ANTIGONA_GATEWAY_TOKEN", None)
        env.pop("ANTIGONA_PIN", None)
        env.pop("ANTIGONA_DEV_TOKENS", None)
        env["ANTIGONA_PROJECT_ROOT"] = tmpdir
        env["PYTHONPATH"] = str((paths.project_root() / "src").resolve())
        result = subprocess.run(
            [sys.executable, "-m", "antigona.cli", "chat"],
            input="/quit\n",
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            cwd=tmpdir,
        )
        assert "Error: Gateway token is required" in (result.stderr + result.stdout)


class TestChatAsyncStartup:
    """Async startup — no RuntimeWarning, no Application.run_async warning."""

    def test_no_runtimewarning_on_startup(self) -> None:
        """No 'run_async was never awaited' warning."""
        result = _antigona_cli("chat", "--token", "test-token")
        assert "run_async" not in (result.stdout + result.stderr)
        assert "RuntimeWarning" not in (result.stdout + result.stderr)

    def test_no_coroutine_warning(self) -> None:
        """No coroutine-was-never-awaited pattern."""
        result = _antigona_cli("chat", "--token", "test-token")
        assert "was never awaited" not in (result.stdout + result.stderr)


class TestCliErrorBoundary:
    def test_chat_exception_withholds_traceback_and_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        printed: list[str] = []
        monkeypatch.setattr(
            cli.console,
            "print",
            lambda *args, **kwargs: printed.append(" ".join(map(str, args))),
        )

        def fail(coroutine: Any) -> None:
            coroutine.close()
            raise RuntimeError("LEAK_SENTINEL_VALUE")

        monkeypatch.setattr(cli.asyncio, "run", fail)
        with pytest.raises(typer.Exit):
            cli.chat(gateway_url="http://gateway.test", token="synthetic-token", no_anim=True)

        output = "\n".join(printed)
        assert "LEAK_SENTINEL_VALUE" not in output
        assert "Traceback" not in output
        assert "[REDACTED]" in output

    def test_http_error_withholds_response_body_and_url_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        printed: list[str] = []
        monkeypatch.setattr(
            cli.console,
            "print",
            lambda *args, **kwargs: printed.append(" ".join(map(str, args))),
        )
        request = httpx.Request("POST", "http://gateway.test/v1/flows")
        response = httpx.Response(
            422,
            json={"detail": "LEAK_SENTINEL_VALUE"},
            request=request,
        )
        error = httpx.HTTPStatusError("bad response", request=request, response=response)

        with pytest.raises(typer.Exit):
            cli._handle_gateway_error(
                "http://gateway.test/private/LEAK_SENTINEL_VALUE",
                error,
            )

        output = "\n".join(printed)
        assert "LEAK_SENTINEL_VALUE" not in output
        assert "[REDACTED]" in output
