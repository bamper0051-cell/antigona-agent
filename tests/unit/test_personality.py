"""Tests for Personality system — SOUL.md, AGENTS.md, Persona."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from antigona.soul import PersonalityManager, PersonalityNotFoundError


class TestPersonalityManager:
    """Test PersonalityManager operations."""

    def test_load_default_personality(self) -> None:
        """Should load default.md from the antigona personalities dir."""
        pm = PersonalityManager()
        persona = pm.load_personality("default")
        assert "Antigona" in persona
        assert len(persona) > 50

    def test_load_concise_personality(self) -> None:
        pm = PersonalityManager()
        persona = pm.load_personality("concise")
        assert persona  # non-empty

    def test_load_teacher_personality(self) -> None:
        pm = PersonalityManager()
        persona = pm.load_personality("teacher")
        assert persona

    def test_load_technical_personality(self) -> None:
        pm = PersonalityManager()
        persona = pm.load_personality("technical")
        assert persona

    def test_load_creative_personality(self) -> None:
        pm = PersonalityManager()
        persona = pm.load_personality("creative")
        assert persona

    def test_list_personalities(self) -> None:
        pm = PersonalityManager()
        pers = pm.list_personalities()
        assert "default" in pers
        assert "concise" in pers
        assert "creative" in pers
        assert "teacher" in pers
        assert "technical" in pers
        # Should have at least the built-in ones
        assert len(pers) >= 5

    def test_current_personality_default(self) -> None:
        pm = PersonalityManager()
        assert pm.current_personality == "default"

    def test_current_personality_after_switch(self) -> None:
        pm = PersonalityManager()
        pm.load_personality("teacher")
        assert pm.current_personality == "teacher"

    def test_switch_personality(self) -> None:
        pm = PersonalityManager()
        text = pm.load_personality("teacher")
        assert "наставник" in text or "Antigona" in text or "объясняй" in text
        assert pm.current_personality == "teacher"

        pm.load_personality("concise")
        assert pm.current_personality == "concise"

    def test_load_nonexistent_personality(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersonalityManager(antigona_dir=tmpdir)
            with pytest.raises(PersonalityNotFoundError):
                pm.load_personality("nonexistent")

    def test_read_soul(self) -> None:
        pm = PersonalityManager()
        soul = pm.read_soul()
        assert "Antigona" in soul or soul == ""  # might be empty in test
    def test_build_system_prompt(self) -> None:
        pm = PersonalityManager()
        prompt = pm.build_system_prompt("default")
        assert "Antigona" in prompt


class TestPersonalityManagerWithCustomDir:
    """Test with a custom temporary directory."""

    def test_custom_personalities(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            antigona_dir = Path(tmpdir)

            # Create a custom personality
            pers_dir = antigona_dir / "personalities"
            pers_dir.mkdir(parents=True, exist_ok=True)
            (pers_dir / "custom.md").write_text("Ты — кастомный агент.", encoding="utf-8")

            # Create SOUL.md
            (antigona_dir / "SOUL.md").write_text("# Личность", encoding="utf-8")

            pm = PersonalityManager(antigona_dir=antigona_dir)
            persona = pm.load_personality("custom")
            assert persona == "Ты — кастомный агент."

            pers = pm.list_personalities()
            assert "custom" in pers

            soul = pm.read_soul()
            assert "# Личность" in soul

            prompt = pm.build_system_prompt("custom")
            assert "кастомный агент" in prompt
            assert "Личность" in prompt

    def test_fallback_to_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            antigona_dir = Path(tmpdir)
            pers_dir = antigona_dir / "personalities"
            pers_dir.mkdir(parents=True, exist_ok=True)

            # Only default.md exists
            (pers_dir / "default.md").write_text("Default fallback", encoding="utf-8")

            pm = PersonalityManager(antigona_dir=antigona_dir)
            persona = pm.load_personality("teacher")
            assert persona == "Default fallback"
