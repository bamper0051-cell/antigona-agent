"""Единая память ядра (Step 5-6): БД-память в ContextBuilder и MEMORIZE в DialogueEngine."""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.context.builder import ContextBuilder
from antigona.core.memory_repository import MemoryRepository
from antigona.database import Database


@pytest.fixture
def memory_repo(tmp_path: Path) -> MemoryRepository:
    db = Database(f"sqlite:///{tmp_path / 'memory_core.db'}")
    db.create_all()
    return MemoryRepository(db)


class TestContextBuilderDbMemory:
    def test_db_memory_injected_for_owner(
        self, memory_repo: MemoryRepository
    ) -> None:
        """Память владельца попадает в системный промпт."""
        memory_repo.remember("owner-1", "Проект использует uv", kind="memory")
        memory_repo.remember("owner-1", "Алиса любит эмодзи", kind="user")
        memory_repo.remember("owner-2", "чужая запись", kind="memory")

        builder = ContextBuilder(memory_repository=memory_repo)
        messages = builder.build(memory_owner_id="owner-1")
        system = messages[0]["content"]

        assert "--- ПАМЯТЬ АГЕНТА ---" in system
        assert "Проект использует uv" in system
        assert "--- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ---" in system
        assert "Алиса любит эмодзи" in system
        # Чужая запись не подмешивается (owner-scoped)
        assert "чужая запись" not in system

    def test_db_memory_skipped_without_owner(
        self, memory_repo: MemoryRepository
    ) -> None:
        """Без memory_owner_id БД-память не подмешивается."""
        marker = "униксекрет-zz99-бд"
        memory_repo.remember("owner-1", marker, kind="memory")
        builder = ContextBuilder(
            memory_repository=memory_repo, long_term_memory=object()
        )
        messages = builder.build()
        assert marker not in messages[0]["content"]
        # С owner-id — подмешивается
        messages2 = builder.build(memory_owner_id="owner-1")
        assert marker in messages2[0]["content"]

    def test_preferences_join_profile(
        self, memory_repo: MemoryRepository
    ) -> None:
        memory_repo.remember("o", "предпочтение: тихий чат", kind="preference")
        builder = ContextBuilder(memory_repository=memory_repo)
        system = builder.build(memory_owner_id="o")[0]["content"]
        assert "предпочтение: тихий чат" in system

    def test_without_repository_no_db_blocks(
        self, tmp_path: Path
    ) -> None:
        # long_term_memory=object() отключает файловую память — проверяем
        # только отсутствие БД-блоков.
        builder = ContextBuilder(long_term_memory=object())
        system = builder.build(memory_owner_id="someone")[0]["content"]
        assert "--- ПАМЯТЬ АГЕНТА ---" not in system
        assert "--- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ---" not in system


class TestDialogueEngineMemorize:
    @pytest.mark.asyncio
    async def test_memorize_stored_and_removed_from_reply(
        self, memory_repo: MemoryRepository
    ) -> None:
        """MEMORIZE-команды сохраняются в БД и убираются из ответа."""
        from antigona.conversation.dialogue_engine import DialogueEngine

        async with DialogueEngine(database=memory_repo._database) as engine:
            reply = (
                "Запомнил! 🙂\n"
                "MEMORIZE|user|Алиса предпочитает краткие ответы\n"
                "MEMORIZE|memory|Проект собирается через uv sync"
            )
            cleaned, stored = engine._extract_and_store_memorize(reply, "owner-9")

            assert stored == 2
            assert "MEMORIZE" not in cleaned
            assert "Запомнил!" in cleaned

            facts = memory_repo.list_entries("owner-9", limit=10)
            contents = {item["content"] for item in facts}
            assert "Алиса предпочитает краткие ответы" in contents
            assert "Проект собирается через uv sync" in contents
            kinds = {item["kind"] for item in facts}
            assert kinds == {"user", "memory"}

    @pytest.mark.asyncio
    async def test_memorize_skipped_without_repository(
        self, tmp_path: Path
    ) -> None:
        from antigona.conversation.dialogue_engine import DialogueEngine

        async with DialogueEngine() as engine:
            cleaned, stored = engine._extract_and_store_memorize(
                "MEMORIZE|user|факт без репозитория", "owner-x"
            )
            assert stored == 0
            assert cleaned == ""

    @pytest.mark.asyncio
    async def test_reply_injects_db_memory(
        self, memory_repo: MemoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reply() подмешивает БД-память сессии в промпт модели."""
        from antigona.conversation.dialogue_engine import DialogueEngine

        memory_repo.remember("u1", "Пользователь u1 — разработчик", kind="user")

        captured: dict[str, object] = {}

        class _FakeProvider:
            def generate(self, messages: list[dict[str, str]]) -> str:
                captured["system"] = messages[0]["content"]
                return "Привет!"

        async with DialogueEngine(database=memory_repo._database, provider=_FakeProvider()) as engine:
            reply = await engine.reply("привет", session_id="telegram:u1")

            assert reply == "Привет!"
            system = str(captured.get("system", ""))
            assert "Пользователь u1 — разработчик" in system
