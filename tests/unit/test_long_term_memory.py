"""Tests for LongTermMemory — facts, preferences, extraction, context builder integration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from antigona.context.builder import ContextBuilder
from antigona.memory.file_memory import FileMemory
from antigona.memory.long_term import (
    LongTermMemory,
    parse_extracted_facts,
)


@pytest.fixture
def db_path():
    """Create a temporary database path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def memory(db_path: str) -> LongTermMemory:
    """Create a LongTermMemory instance backed by a temp DB."""
    return LongTermMemory(db_path=db_path)


# ─── LongTermMemory: facts ────────────────────────────────────────────────────


class TestLongTermMemoryFacts:
    def test_save_and_get_fact(self, memory: LongTermMemory) -> None:
        memory.save_fact("user_name", "Алиса")
        assert memory.get_fact("user_name") == "Алиса"

    def test_get_fact_not_found(self, memory: LongTermMemory) -> None:
        assert memory.get_fact("nonexistent") == ""

    def test_update_fact(self, memory: LongTermMemory) -> None:
        memory.save_fact("user_name", "Алиса")
        memory.save_fact("user_name", "Боб")
        assert memory.get_fact("user_name") == "Боб"

    def test_save_fact_with_category(self, memory: LongTermMemory) -> None:
        memory.save_fact("project_name", "Antigona", category="project")
        assert memory.get_fact("project_name", category="project") == "Antigona"
        # Different category = different key namespace
        assert memory.get_fact("project_name") == ""

    def test_get_all_facts_empty(self, memory: LongTermMemory) -> None:
        assert memory.get_all_facts() == {}

    def test_get_all_facts(self, memory: LongTermMemory) -> None:
        memory.save_fact("user_name", "Алиса", category="user")
        memory.save_fact("project_name", "Antigona", category="project")
        facts = memory.get_all_facts()
        assert "user" in facts
        assert facts["user"]["user_name"] == "Алиса"
        assert "project" in facts
        assert facts["project"]["project_name"] == "Antigona"

    def test_delete_fact(self, memory: LongTermMemory) -> None:
        memory.save_fact("user_name", "Алиса")
        assert memory.delete_fact("user_name") is True
        assert memory.get_fact("user_name") == ""

    def test_delete_fact_not_found(self, memory: LongTermMemory) -> None:
        assert memory.delete_fact("nonexistent") is False

    def test_save_facts_bulk(self, memory: LongTermMemory) -> None:
        facts = [
            {"key": "user_name", "value": "Алиса", "category": "user"},
            {"key": "project_name", "value": "Antigona", "category": "project"},
        ]
        count = memory.save_facts_bulk(facts)
        assert count == 2
        assert memory.get_fact("user_name") == "Алиса"
        assert memory.get_fact("project_name", category="project") == "Antigona"

    def test_save_facts_bulk_skips_empty(self, memory: LongTermMemory) -> None:
        facts = [
            {"key": "", "value": "test", "category": "user"},
            {"key": "valid", "value": "", "category": "user"},
            {"key": "ok", "value": "yes", "category": "user"},
        ]
        count = memory.save_facts_bulk(facts)
        assert count == 1
        assert memory.get_fact("ok") == "yes"


# ─── LongTermMemory: preferences ──────────────────────────────────────────────


class TestLongTermMemoryPreferences:
    def test_save_and_get_preference(self, memory: LongTermMemory) -> None:
        memory.save_preference("style_emojis", "true")
        assert memory.get_preference("style_emojis") == "true"

    def test_get_preference_not_found(self, memory: LongTermMemory) -> None:
        assert memory.get_preference("nonexistent") == ""

    def test_update_preference(self, memory: LongTermMemory) -> None:
        memory.save_preference("style_emojis", "true")
        memory.save_preference("style_emojis", "false")
        assert memory.get_preference("style_emojis") == "false"

    def test_get_all_preferences(self, memory: LongTermMemory) -> None:
        memory.save_preference("style_emojis", "true")
        memory.save_preference("style_brief", "true")
        prefs = memory.get_all_preferences()
        assert prefs["style_emojis"] == "true"
        assert prefs["style_brief"] == "true"

    def test_delete_preference(self, memory: LongTermMemory) -> None:
        memory.save_preference("style_emojis", "true")
        assert memory.delete_preference("style_emojis") is True
        assert memory.get_preference("style_emojis") == ""

    def test_save_preferences_bulk(self, memory: LongTermMemory) -> None:
        prefs = {"style_emojis": "true", "style_brief": "false"}
        count = memory.save_preferences_bulk(prefs)
        assert count == 2
        assert memory.get_preference("style_emojis") == "true"

    def test_has_memory_false(self, memory: LongTermMemory) -> None:
        assert memory.has_memory() is False

    def test_has_memory_true(self, memory: LongTermMemory) -> None:
        memory.save_fact("user_name", "Алиса")
        assert memory.has_memory() is True


# ─── LongTermMemory: preferences block ────────────────────────────────────────


class TestLongTermMemoryBlock:
    def test_get_preferences_block_empty(self, memory: LongTermMemory) -> None:
        block = memory.get_preferences_block()
        assert block == ""

    def test_get_preferences_block_with_facts(self, memory: LongTermMemory) -> None:
        memory.save_fact("user_name", "Алиса", category="user")
        block = memory.get_preferences_block()
        assert "ФАКТЫ О ПОЛЬЗОВАТЕЛЕ" in block
        assert "Алиса" in block

    def test_get_preferences_block_with_prefs(self, memory: LongTermMemory) -> None:
        memory.save_preference("style_emojis", "true")
        block = memory.get_preferences_block()
        assert "ПРЕДПОЧТЕНИЯ СТИЛЯ" in block
        assert "эмодзи" in block

    def test_get_preferences_block_combined(self, memory: LongTermMemory) -> None:
        memory.save_fact("user_name", "Алиса", category="user")
        memory.save_preference("style_brief", "true")
        block = memory.get_preferences_block()
        assert "ФАКТЫ О ПОЛЬЗОВАТЕЛЕ" in block
        assert "ПРЕДПОЧТЕНИЯ СТИЛЯ" in block
        assert "Алиса" in block
        assert "кратко" in block


# ─── Fact extraction parser ───────────────────────────────────────────────────


class TestParseExtractedFacts:
    def test_parse_fact_line(self) -> None:
        text = "FACT|user_name|Алиса|user"
        result = parse_extracted_facts(text)
        assert "no_facts" not in result or not result["no_facts"]
        assert len(result["facts"]) == 1
        assert result["facts"][0] == {
            "key": "user_name",
            "value": "Алиса",
            "category": "user",
        }

    def test_parse_fact_multiple(self) -> None:
        text = (
            "FACT|user_name|Алиса|user\n"
            "FACT|project_name|Antigona|project\n"
            "PREFERENCE|style_emojis|true"
        )
        result = parse_extracted_facts(text)
        assert len(result["facts"]) == 2
        assert result["preferences"]["style_emojis"] == "true"

    def test_parse_no_facts(self) -> None:
        result = parse_extracted_facts("NO_FACTS")
        assert result.get("no_facts") is True

    def test_parse_empty_input(self) -> None:
        result = parse_extracted_facts("")
        assert result.get("no_facts") is True

    def test_parse_whitespace_only(self) -> None:
        result = parse_extracted_facts("   ")
        assert result.get("no_facts") is True

    def test_parse_case_insensitive_no_facts(self) -> None:
        result = parse_extracted_facts("no_facts")
        assert result.get("no_facts") is True

    def test_parse_bad_lines_ignored(self) -> None:
        text = (
            "some random text\n"
            "FACT|user_name|Алиса|user\n"
            "INVALID|stuff\n"
            "PREFERENCE|style_emojis|true"
        )
        result = parse_extracted_facts(text)
        assert len(result["facts"]) == 1
        assert result["preferences"]["style_emojis"] == "true"

    def test_parse_preference_only(self) -> None:
        text = "PREFERENCE|style_emojis|true"
        result = parse_extracted_facts(text)
        assert len(result["facts"]) == 0
        assert result["preferences"]["style_emojis"] == "true"

    def test_parse_mixed_no_facts(self) -> None:
        text = "FACT|user_name|Алиса|user\nNO_FACTS"
        result = parse_extracted_facts(text)
        # no_facts is True but facts are also parsed
        assert result.get("no_facts") is True
        assert len(result["facts"]) == 1

    def test_parse_fact_missing_category(self) -> None:
        text = "FACT|user_name|Алиса|user"
        result = parse_extracted_facts(text)
        assert result["facts"][0]["category"] == "user"


# ─── ContextBuilder integration ───────────────────────────────────────────────


class TestContextBuilderWithMemory:
    @pytest.fixture
    def isolated_memory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileMemory:
        # FileMemory reads stores via module-global _STORE_FILES; redirect them
        # to a temp dir so the test is isolated from the real MEMORY.md/USER.md.
        from antigona.memory import file_memory as _fm

        monkeypatch.setitem(_fm._STORE_FILES, "memory", tmp_path / "MEMORY.md")
        monkeypatch.setitem(_fm._STORE_FILES, "user", tmp_path / "USER.md")
        return FileMemory()

    def test_build_with_memory_injects_block(self, isolated_memory: FileMemory) -> None:
        isolated_memory.add_entry("user", "user_name", "Алиса")
        builder = ContextBuilder(file_memory=isolated_memory)
        messages = builder.build()
        assert len(messages) == 1
        content = messages[0]["content"]
        assert "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" in content
        assert "Алиса" in content

    def test_build_without_memory_no_block(self, isolated_memory: FileMemory) -> None:
        builder = ContextBuilder(file_memory=isolated_memory)
        messages = builder.build()
        content = messages[0]["content"]
        assert "ПАМЯТЬ АГЕНТА" not in content
        assert "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" not in content

    def test_build_with_preferences_injects_block(self, isolated_memory: FileMemory) -> None:
        isolated_memory.add_entry("memory", "style", "эмодзи")
        builder = ContextBuilder(file_memory=isolated_memory)
        messages = builder.build()
        content = messages[0]["content"]
        assert "ПАМЯТЬ АГЕНТА" in content
        assert "эмодзи" in content


# ─── Context compression ──────────────────────────────────────────────────────


class TestContextCompression:
    def test_should_compress_under_threshold(self) -> None:
        from antigona.memory.summarizer import MemorySummarizer

        mem = MemorySummarizer(buffer_size=20)
        # Push 10 turns — should not compress
        for i in range(10):
            mem.push_user_turn(f"message {i}")
        assert mem.should_compress() is False

    def test_should_compress_over_threshold(self) -> None:
        from antigona.memory.summarizer import MemorySummarizer

        mem = MemorySummarizer(buffer_size=20)
        # Push 16 turns — should compress
        for i in range(16):
            mem.push_user_turn(f"message {i}")
        assert mem.should_compress() is True

    def test_get_compressible_turns_returns_oldest(self) -> None:
        from antigona.memory.summarizer import MemorySummarizer

        mem = MemorySummarizer(buffer_size=20)
        for i in range(20):
            mem.push_user_turn(f"message {i}")
        old = mem.get_compressible_turns(10)
        assert len(old) == 10
        assert old[0]["content"] == "message 0"
        assert old[9]["content"] == "message 9"

    def test_get_compressible_turns_not_enough(self) -> None:
        from antigona.memory.summarizer import MemorySummarizer

        mem = MemorySummarizer(buffer_size=20)
        for i in range(5):
            mem.push_user_turn(f"message {i}")
        old = mem.get_compressible_turns(10)
        assert len(old) == 0

    def test_apply_compression_removes_old_turns(self) -> None:
        from antigona.memory.summarizer import MemorySummarizer

        mem = MemorySummarizer(buffer_size=20)
        for i in range(20):
            mem.push_user_turn(f"message {i}")
        old = mem.get_compressible_turns(10)
        mem.apply_compression(old, "User sent 10 messages about testing")
        assert len(mem.turn_buffer) == 10
        assert mem.turn_buffer[0]["content"] == "message 10"
        assert "Compressed" in mem.session_summary

    def test_apply_compression_empty(self) -> None:
        from antigona.memory.summarizer import MemorySummarizer

        mem = MemorySummarizer(buffer_size=20)
        mem.apply_compression([], "empty")
        # No change
        assert len(mem.turn_buffer) == 0

    def test_persistence_across_instances(self, db_path: str) -> None:
        """Verify facts survive across LongTermMemory instances (SQLite persistence)."""
        mem1 = LongTermMemory(db_path=db_path)
        mem1.save_fact("user_name", "Алиса")
        mem1.close()

        mem2 = LongTermMemory(db_path=db_path)
        assert mem2.get_fact("user_name") == "Алиса"
        mem2.close()
