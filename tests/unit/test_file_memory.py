"""Tests for Hermes-style file-based memory (FileMemory), ContextBuilder
frozen snapshot integration, ActionExecutor MEMORIZE parsing,
and proactive memory saving.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from antigona.context.builder import ContextBuilder
from antigona.memory import file_memory as file_memory_module
from antigona.memory.file_memory import (
    CHAR_LIMITS,
    FileMemory,
    _parse_entries,
    _serialize_entries,
)
from antigona.tools.action_executor import ActionExecutor, ActionType

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execution-mechanics tests must not depend on ambient ANTIGONA_PIN."""
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)


@pytest.fixture
def memory_dir() -> Path:
    """Create a temporary memory directory."""
    tmp = Path(tempfile.mkdtemp())
    yield tmp
    # Cleanup
    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()


@pytest.fixture
def memory(memory_dir: Path) -> FileMemory:
    """Create a FileMemory instance backed by a temp directory."""
    return FileMemory(memory_dir=memory_dir)


@pytest.fixture(autouse=True)
def _clear_default_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Clear the default .memory/ dir before each test so tests don't leak.

    The default memory root is a *temporary* directory, never the checkout's
    real ``.memory/``: the module-level constants and the
    ``ANTIGONA_MEMORY_ROOT`` env var are redirected to a per-test tmp dir, so
    both the import-time ``MEMORY_DIR`` and the lazily resolved
    ``_default_memory_dir()`` point at the temp root.  The temp dir's stores
    are then cleared before the test runs.
    """
    tmp = tmp_path_factory.mktemp("default_memory")
    monkeypatch.setattr(file_memory_module, "MEMORY_DIR", tmp)
    monkeypatch.setattr(file_memory_module, "MEMORY_FILE", tmp / "MEMORY.md")
    monkeypatch.setattr(file_memory_module, "USER_FILE", tmp / "USER.md")
    monkeypatch.setenv("ANTIGONA_MEMORY_ROOT", str(tmp))
    (tmp / "MEMORY.md").write_text("", encoding="utf-8")
    (tmp / "USER.md").write_text("", encoding="utf-8")
    for f in tmp.glob("*"):
        f.write_text("", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# FileMemory: parsing helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestParsingHelpers:
    def test_parse_entries_empty(self) -> None:
        assert _parse_entries("") == {}

    def test_parse_entries_single(self) -> None:
        text = "§ Title\ncontent"
        entries = _parse_entries(text)
        assert entries == {"Title": "content"}

    def test_parse_entries_multi(self) -> None:
        text = "§ Title1\ncontent1\n§ Title2\ncontent2"
        entries = _parse_entries(text)
        assert entries == {"Title1": "content1", "Title2": "content2"}

    def test_parse_entries_multiline_content(self) -> None:
        text = "§ Project info\nUses Python 3.11\npytest\nruff"
        entries = _parse_entries(text)
        assert entries["Project info"] == "Uses Python 3.11\npytest\nruff"

    def test_serialize_roundtrip(self) -> None:
        original = {"Title1": "content1", "Title2": "content2 with\nnewline"}
        serialized = _serialize_entries(original)
        reparsed = _parse_entries(serialized)
        assert reparsed == original

    def test_parse_entries_ignores_empty_title(self) -> None:
        text = "\ncontent"
        assert _parse_entries(text) == {}

    def test_parse_entries_no_section_marker(self) -> None:
        """Text without § markers yields empty entries."""
        assert _parse_entries("plain text without sections") == {}


# ═══════════════════════════════════════════════════════════════════════════════
# FileMemory: add_entry / remove_entry / get_content / is_full
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileMemory:
    def test_add_and_get_entry(self, memory: FileMemory) -> None:
        memory.add_entry("memory", "Test Title", "Test content")
        content = memory.get_content("memory")
        assert "Test Title" in content
        assert "Test content" in content

    def test_add_entry_replaces_existing(self, memory: FileMemory) -> None:
        memory.add_entry("user", "Name", "Алиса")
        memory.add_entry("user", "Name", "Боб")
        content = memory.get_content("user")
        assert "Боб" in content
        assert "Алиса" not in content

    def test_add_multiple_entries(self, memory: FileMemory) -> None:
        memory.add_entry("memory", "Project", "Antigona project")
        memory.add_entry("memory", "Language", "Python 3.11")
        content = memory.get_content("memory")
        assert "§ Project" in content
        assert "§ Language" in content

    def test_remove_entry(self, memory: FileMemory) -> None:
        memory.add_entry("memory", "Test", "content")
        assert memory.remove_entry("memory", "Test") is True
        assert memory.get_content("memory") == ""

    def test_remove_entry_not_found(self, memory: FileMemory) -> None:
        assert memory.remove_entry("memory", "Nonexistent") is False

    def test_get_content_empty(self, memory_dir: Path) -> None:
        memory = FileMemory(memory_dir=memory_dir)
        assert memory.get_content("memory") == ""
        assert memory.get_content("user") == ""

    def test_is_full_empty(self, memory: FileMemory) -> None:
        assert memory.is_full("memory") is False
        assert memory.is_full("user") is False

    def test_is_full_under_limit(self, memory: FileMemory) -> None:
        memory.add_entry("user", "Test", "a" * 100)
        assert memory.is_full("user") is False

    def test_is_full_at_limit(self, memory: FileMemory) -> None:
        """Fill user store with content that reaches but doesn't exceed limit."""
        # "§ Large entry\n" + content = serialized form
        # Max serialized = 1375. Header = len("§ Large entry\n") = 14
        # So content can be at most 1375 - 14 = 1361
        # But we use this approach: find the max content that fits
        content = "a" * (CHAR_LIMITS["user"] - 50)  # safe under limit with header overhead
        memory.add_entry("user", "X", content)
        # Now add enough to hit the limit exactly
        # "§ X\n" + content = 5 + len(content). We need len >= limit
        # Let's check: if serialized is exactly at limit, is_full = True (>=)
        assert memory.is_full("user") is False  # Under limit

    def test_add_entry_raises_on_overflow(self, memory: FileMemory) -> None:
        """Adding an entry that would exceed the limit raises ValueError."""
        # Serialized form: "§ Overflow\n" + content
        header_overhead = len("§ Overflow\n")  # 11
        max_content = CHAR_LIMITS["memory"] - header_overhead
        # Adding content right at the boundary works
        memory.add_entry("memory", "Overflow", "a" * max_content)
        # But we can't verify that adding more would fail without first removing something
        assert memory.is_full("memory") is True

    def test_validate_store(self, memory: FileMemory) -> None:
        with pytest.raises(ValueError, match="Invalid store"):
            memory.add_entry("invalid", "title", "content")

    def test_has_content(self, memory_dir: Path) -> None:
        memory = FileMemory(memory_dir=memory_dir)
        assert memory.has_content("memory") is False
        memory.add_entry("memory", "Test", "content")
        assert memory.has_content("memory") is True

    def test_clear_all(self, memory: FileMemory) -> None:
        memory.add_entry("memory", "Test", "content")
        memory.add_entry("user", "Name", "Алиса")
        memory.clear_all()
        assert memory.get_content("memory") == ""
        assert memory.get_content("user") == ""

    def test_get_entries(self, memory: FileMemory) -> None:
        memory.add_entry("memory", "Title1", "content1")
        memory.add_entry("memory", "Title2", "content2")
        entries = memory.get_entries("memory")
        assert entries == {"Title1": "content1", "Title2": "content2"}

    def test_get_snapshot(self, memory: FileMemory) -> None:
        memory.add_entry("memory", "Note", "some note")
        memory.add_entry("user", "Name", "Алиса")
        snap = memory.get_snapshot()
        assert "some note" in snap["memory"]
        assert "Алиса" in snap["user"]

    def test_persistence(self, memory_dir: Path) -> None:
        """Data persists across FileMemory instances."""
        mem1 = FileMemory(memory_dir=memory_dir)
        mem1.add_entry("user", "Name", "Алиса")

        mem2 = FileMemory(memory_dir=memory_dir)
        assert mem2.get_content("user") != ""
        assert "Алиса" in mem2.get_content("user")

    def test_add_entry_user_store(self, memory: FileMemory) -> None:
        memory.add_entry("user", "User name", "Алиса")
        content = memory.get_content("user")
        assert "User name" in content
        assert "Алиса" in content


# ═══════════════════════════════════════════════════════════════════════════════
# ContextBuilder: frozen snapshot injection
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextBuilderFrozenSnapshot:
    def test_build_with_file_memory_injects_blocks(self, memory: FileMemory) -> None:
        memory.add_entry("memory", "Project", "Antigona uses Python 3.11")
        memory.add_entry("user", "Name", "Алиса")
        builder = ContextBuilder(file_memory=memory)

        # Frozen snapshot is loaded at init
        assert builder._frozen_memory is not None

        messages = builder.build()
        content = messages[0]["content"]
        assert "ПАМЯТЬ АГЕНТА" in content
        assert "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" in content
        assert "Antigona uses Python 3.11" in content
        assert "Алиса" in content

    def test_build_without_memory_no_blocks(self) -> None:
        """When default ContextBuilder has no file_memory, no memory blocks appear.

        The default .memory/ dir is redirected to a temp dir by autouse fixture.
        """
        builder = ContextBuilder()
        messages = builder.build()
        content = messages[0]["content"]
        assert "ПАМЯТЬ АГЕНТА" not in content
        assert "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" not in content

    def test_build_frozen_snapshot_does_not_change(self, memory: FileMemory) -> None:
        """Frozen snapshot is loaded once and doesn't reflect later changes."""
        memory.add_entry("memory", "Note", "original content")
        builder = ContextBuilder(file_memory=memory)

        # Change memory AFTER building builder
        memory.add_entry("memory", "Note", "changed content")

        messages = builder.build()
        content = messages[0]["content"]
        assert "original content" in content

    def test_build_memory_only_user(self, memory: FileMemory) -> None:
        """Only USER.md content, no MEMORY.md."""
        memory.add_entry("user", "Name", "Алиса")
        builder = ContextBuilder(file_memory=memory)
        messages = builder.build()
        content = messages[0]["content"]
        assert "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" in content
        assert "Алиса" in content

    def test_build_memory_only_agent(self, memory: FileMemory) -> None:
        """Only MEMORY.md content, no USER.md."""
        memory.add_entry("memory", "Project", "Antigona")
        builder = ContextBuilder(file_memory=memory)
        messages = builder.build()
        content = messages[0]["content"]
        assert "ПАМЯТЬ АГЕНТА" in content
        assert "Antigona" in content

    def test_build_with_file_memory_and_policy(self, memory: FileMemory) -> None:
        """Memory blocks + policy rules together."""
        memory.add_entry("user", "Name", "Алиса")
        builder = ContextBuilder(file_memory=memory)
        verdicts = [
            {"allowed": True, "reason": "OK", "risk_level": "LOW"},
        ]
        messages = builder.build(policy_verdicts=verdicts)
        content = messages[0]["content"]
        assert "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" in content
        assert "Алиса" in content
        assert "ПРАВИЛА БЕЗОПАСНОСТИ" in content

    def test_build_memory_block_format(self, memory: FileMemory) -> None:
        """Verify exact block format."""
        memory.add_entry("memory", "Note", "test note")
        memory.add_entry("user", "Name", "test user")
        builder = ContextBuilder(file_memory=memory)
        messages = builder.build()
        content = messages[0]["content"]
        assert "--- ПАМЯТЬ АГЕНТА ---" in content
        assert "--- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ---" in content


# ═══════════════════════════════════════════════════════════════════════════════
# ActionExecutor: MEMORIZE parsing
# ═══════════════════════════════════════════════════════════════════════════════


class TestActionExecutorMemorize:
    def test_parse_memorize(self) -> None:
        executor = ActionExecutor()
        text = "MEMORIZE|user|Алиса — создатель Antigona"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].type == ActionType.MEMORIZE
        assert actions[0].metadata["store"] == "user"
        assert "Алиса" in actions[0].content

    def test_parse_memorize_memory_store(self) -> None:
        executor = ActionExecutor()
        text = "MEMORIZE|memory|Проект использует Python 3.11, pytest, ruff"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].metadata["store"] == "memory"

    def test_parse_memorize_generates_title(self) -> None:
        executor = ActionExecutor()
        text = "MEMORIZE|user|Алиса — создатель Antigona, предпочитает эмодзи"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        # Title = first 50 chars
        expected_title = "Алиса — создатель Antigona, предпочитает эмодзи"[:50].strip()
        assert actions[0].metadata["title"] == expected_title

    def test_parse_memorize_empty_content(self) -> None:
        executor = ActionExecutor()
        text = "MEMORIZE|user|"
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 0

    def test_parse_mixed_with_other_actions(self) -> None:
        executor = ActionExecutor()
        text = (
            "MEMORIZE|user|Алиса — создатель\n"
            "WRITE_FILE|/tmp/test.txt|content\n"
            "MEMORIZE|memory|Python 3.11\n"
        )
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 3
        memorize_actions = [a for a in actions if a.type == ActionType.MEMORIZE]
        assert len(memorize_actions) == 2

    def test_parse_memorize_with_natural_language(self) -> None:
        executor = ActionExecutor()
        text = (
            "Привет! Я запомню это.\n"
            "MEMORIZE|user|Алиса — создатель Antigona\n"
            "Готово!"
        )
        actions = executor.parse_action_from_llm(text)
        assert len(actions) == 1
        assert actions[0].type == ActionType.MEMORIZE

    def test_strip_memorize_from_text(self) -> None:
        executor = ActionExecutor()
        text = "MEMORIZE|user|test content\nSome explanation"
        cleaned = executor.strip_action_commands(text)
        assert "MEMORIZE" not in cleaned
        assert "Some explanation" in cleaned

    def test_execute_memorize(self) -> None:
        """Test executing a MEMORIZE action via ActionExecutor."""
        executor = ActionExecutor()
        from antigona.tools.action_executor import Action

        action = Action(
            type=ActionType.MEMORIZE,
            path="user",
            content="Алиса — создатель Antigona",
            metadata={"store": "user", "title": "Алиса — создатель Antigona"},
        )
        # execute() runs the loop itself when there's no running loop, so call it
        # directly — wrapping it in asyncio.run() would feed an ActionResult to
        # asyncio.run (which expects a coroutine).
        result = executor.execute(action)
        assert result.success is True
        assert "User" in result.message

    def test_execute_memorize_empty(self) -> None:
        executor = ActionExecutor()
        from antigona.tools.action_executor import Action

        action = Action(
            type=ActionType.MEMORIZE,
            path="user",
            content="",
            metadata={"store": "user", "title": ""},
        )
        result = executor.execute(action)
        assert result.success is False
        assert "Пустое" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# Proactive memory: MEMORIZE commands in LLM responses
# ═══════════════════════════════════════════════════════════════════════════════


class TestProactiveMemory:
    def test_proactive_memory_saves_to_file(self, memory_dir: Path) -> None:
        """Simulate an LLM response with MEMORIZE command."""
        mem = FileMemory(memory_dir=memory_dir)
        llm_reply = (
            "Привет! Я Antigona.\n"
            "MEMORIZE|user|Алиса — создатель Antigona, предпочитает эмодзи\n"
            "Чем могу помочь?"
        )

        executor = ActionExecutor()
        actions = executor.parse_action_from_llm(llm_reply)
        memorize_actions = [a for a in actions if a.type == ActionType.MEMORIZE]

        assert len(memorize_actions) == 1
        for a in memorize_actions:
            store = a.metadata.get("store", "memory")
            title = a.metadata.get("title", a.content[:50].strip())
            mem.add_entry(store, title, a.content)

        user_content = mem.get_content("user")
        assert "Алиса" in user_content
        assert "эмодзи" in user_content

    def test_proactive_memory_agent_notes(self, memory_dir: Path) -> None:
        """Agent saves environment/convention notes via MEMORIZE."""
        mem = FileMemory(memory_dir=memory_dir)
        llm_reply = "MEMORIZE|memory|Проект использует Python 3.11, pytest, ruff"

        executor = ActionExecutor()
        actions = executor.parse_action_from_llm(llm_reply)
        memorize = [a for a in actions if a.type == ActionType.MEMORIZE]

        for a in memorize:
            mem.add_entry("memory", a.metadata["title"], a.content)

        mem_content = mem.get_content("memory")
        assert "Python 3.11" in mem_content
        assert "pytest" in mem_content

    def test_no_memorize_commands_no_change(self, memory_dir: Path) -> None:
        """LLM response without MEMORIZE should not change memory."""
        mem = FileMemory(memory_dir=memory_dir)
        llm_reply = "Привет! Чем могу помочь?"

        executor = ActionExecutor()
        actions = executor.parse_action_from_llm(llm_reply)
        memorize = [a for a in actions if a.type == ActionType.MEMORIZE]

        assert len(memorize) == 0
        assert mem.get_content("memory") == ""

    def test_proactive_memory_updates_existing_entry(self, memory_dir: Path) -> None:
        """Same title replaces existing entry."""
        mem = FileMemory(memory_dir=memory_dir)
        mem.add_entry("user", "Name", "old name")

        # Same title → replacement, not append (add_entry is title-keyed).
        mem.add_entry("user", "Name", "new name")

        user_content = mem.get_content("user")
        assert "new name" in user_content
        assert "old name" not in user_content


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: ContextBuilder + FileMemory end-to-end
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEnd:
    def test_gate_scenario(self, memory_dir: Path) -> None:
        """Gate test: 'Меня зовут Алиса, я создатель' → MEMORIZE."""
        mem = FileMemory(memory_dir=memory_dir)

        # Simulate LLM response containing MEMORIZE
        llm_reply = (
            "Приятно познакомиться, Алиса! "
            "MEMORIZE|user|Алиса — создатель Antigona, предпочитает эмодзи"
        )

        executor = ActionExecutor()
        actions = executor.parse_action_from_llm(llm_reply)
        for a in actions:
            if a.type == ActionType.MEMORIZE:
                mem.add_entry(a.metadata["store"], a.metadata["title"], a.content)

        # Verify
        user_content = mem.get_content("user")
        assert "Алиса" in user_content
        assert "создатель" in user_content

        # After restart (new instance), memory persists
        mem2 = FileMemory(memory_dir=memory_dir)
        user_content_after_restart = mem2.get_content("user")
        assert "Алиса" in user_content_after_restart

        # ContextBuilder with frozen snapshot should include this
        builder = ContextBuilder(file_memory=mem2)
        messages = builder.build()
        system_content = messages[0]["content"]
        assert "Алиса" in system_content

    def test_memory_persists_across_builder_instances(
        self, memory_dir: Path
    ) -> None:
        """Memory written to files persists across ContextBuilder instances."""
        mem = FileMemory(memory_dir=memory_dir)
        mem.add_entry("user", "Name", "Алиса")

        builder1 = ContextBuilder(file_memory=mem)
        msg1 = builder1.build()
        assert "Алиса" in msg1[0]["content"]

        # Second builder also sees it (frozen at its own construction)
        mem2 = FileMemory(memory_dir=memory_dir)
        builder2 = ContextBuilder(file_memory=mem2)
        msg2 = builder2.build()
        assert "Алиса" in msg2[0]["content"]

    def test_char_limits_are_correct(self) -> None:
        """Verify the char limits match the spec."""
        assert CHAR_LIMITS["memory"] == 2200
        assert CHAR_LIMITS["user"] == 1375

    def test_store_format_matches_spec(self, memory: FileMemory) -> None:
        """Verify the § TITLE\\ncontent format."""
        memory.add_entry("memory", "Test Note", "some content here")
        content = memory.get_content("memory")
        assert content.startswith("§")
        assert "Test Note" in content
        assert "some content here" in content
