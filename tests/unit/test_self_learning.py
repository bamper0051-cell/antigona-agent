from __future__ import annotations

from pathlib import Path

import pytest

from antigona.memory.self_learning import LearningRecord, LearningType, SelfLearningTool


@pytest.fixture
def temp_store(tmp_path: Path) -> SelfLearningTool:
    return SelfLearningTool(storage_path=tmp_path / "learnings_test.json")


@pytest.mark.anyio
async def test_store_and_search_learning(temp_store: SelfLearningTool) -> None:
    from datetime import UTC, datetime

    learning = LearningRecord(
        id="test-id-1",
        type=LearningType.USER_PREFERENCE,
        content="В проекте Antigona не использовать Docker.",
        source_task_id="task-1",
        source_message_ids=[123],
        confidence=1.0,
        created_at=datetime.now(UTC),
        last_verified_at=datetime.now(UTC),
        scope="global",
        status="approved",
    )
    await temp_store.store_learning(learning)

    results = await temp_store.search_learnings("Docker")
    assert len(results) == 1
    assert results[0].content == "В проекте Antigona не использовать Docker."


@pytest.mark.anyio
async def test_extract_candidates(temp_store: SelfLearningTool) -> None:
    task_result = (
        "Ошибка X была исправлена путем Y. Пользователь предпочитает использовать systemd вместо Docker.\n"
        "Секретный токен: secret-token-123456"
    )
    candidates = await temp_store.extract_candidates(task_result, "task-2", [124])

    # Heuristics should find rule/preference or error fix
    assert len(candidates) >= 1
    contents = [c.content for c in candidates]

    # Sensitive data (secret token) should not be extracted
    for c in contents:
        assert "secret-token-123456" not in c
        assert "token" not in c.lower()

    assert any("systemd" in c for c in contents)


@pytest.mark.anyio
async def test_invalidate_learning(temp_store: SelfLearningTool) -> None:
    from datetime import UTC, datetime

    learning = LearningRecord(
        id="test-id-delete",
        type=LearningType.USER_PREFERENCE,
        content="Удалить это.",
        source_task_id="task-3",
        source_message_ids=[125],
        confidence=1.0,
        created_at=datetime.now(UTC),
        last_verified_at=datetime.now(UTC),
        scope="global",
        status="approved",
    )
    await temp_store.store_learning(learning)

    deleted = await temp_store.invalidate_learning("test-id-delete")
    assert deleted is True

    results = await temp_store.search_learnings("Удалить")
    assert len(results) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "diagnostic",
    [
        "Ошибка выполнения: /bin/sh: 1: Pwd: not found",
        "[TOOL_ERROR] command failed",
        "EXECUTION_UNKNOWN: shell result unavailable",
        "Traceback (most recent call last): Ошибка выполнения",
        "RuntimeError: fix command failed",
        "Exception: исправлено не было",
        "/bin/sh: 1: pwdx: command not found",
        "sh: 1: pwdx: not found",
        "command-not-found: fix package is unavailable",
    ],
)
async def test_extract_candidates_rejects_raw_execution_diagnostics(
    temp_store: SelfLearningTool,
    diagnostic: str,
) -> None:
    candidates = await temp_store.extract_candidates(diagnostic, "task-diagnostic", [126])

    assert candidates == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "correction",
    [
        "Ошибка исправлена: используйте lowercase pwd",
        "Правило: команды shell следует писать в lowercase",
        "Fix: use lowercase pwd in shell commands",
    ],
)
async def test_extract_candidates_keeps_explicit_durable_corrections(
    temp_store: SelfLearningTool,
    correction: str,
) -> None:
    candidates = await temp_store.extract_candidates(correction, "task-correction", [127])

    assert [candidate.content for candidate in candidates] == [correction]
