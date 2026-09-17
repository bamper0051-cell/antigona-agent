"""Тесты загрузки проектного .env в CLI (ANTIGONA_PIN должен быть виден гейту)."""

from __future__ import annotations

import os
import pathlib

import pytest

from antigona.cli import _load_project_env


@pytest.fixture(autouse=True)
def _clean_pin() -> None:
    os.environ.pop("ANTIGONA_PIN", None)
    yield
    os.environ.pop("ANTIGONA_PIN", None)


def test_load_project_env_populates_pin(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".env").write_text("ANTIGONA_PIN=secret123\n", encoding="utf-8")

    _load_project_env(tmp_path)

    assert os.environ["ANTIGONA_PIN"] == "secret123"


def test_load_project_env_does_not_override_exported_value(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".env").write_text("ANTIGONA_PIN=secret123\n", encoding="utf-8")
    os.environ["ANTIGONA_PIN"] = "sentinel-from-shell"

    _load_project_env(tmp_path)

    assert os.environ["ANTIGONA_PIN"] == "sentinel-from-shell"


def test_load_project_env_missing_file_is_noop(tmp_path: pathlib.Path) -> None:
    _load_project_env(tmp_path)

    assert "ANTIGONA_PIN" not in os.environ


def test_default_root_is_project_root() -> None:
    import antigona.cli as cli

    expected = pathlib.Path(cli.__file__).resolve().parents[2]
    assert (expected / "src" / "antigona" / "cli.py").exists()
