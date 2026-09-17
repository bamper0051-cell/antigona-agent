"""P1-04 (Antigona R2 Codex review): Worker TurnWorkerRuntime staleness after /setllm.

Canonical finding — evidence/hermes_autonomy/T0031/CODEX_REVIEW_CANONICAL.md:

    [P1] src/antigona/worker/__init__.py:372-389,428-435;
    src/antigona/worker/turn_executor.py:92-117,131-153 — Worker строит один
    immutable TurnWorkerRuntime до основного цикла; /setllm после запуска меняет
    state-файл, но следующие tasks продолжают использовать старые
    host/model/credential — runtime расходится с текущим operator selection и
    может продолжить отправку старому провайдеру — тесты каждый раз заново
    вызывают resolver helper и не моделируют живой worker во время
    переключения — blocking
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from antigona.config import Settings
from antigona.database import Database
from antigona.providers.resolver import ProviderResolver, _save_state
from antigona.repository import CreateTask, TaskRepository
from antigona.turn_bridge.turn_engine_adapter import TurnResult
from antigona.turn_bridge.worker_adapter import TurnWorker
from antigona.worker.turn_executor import TurnTaskExecutor, build_turn_runtime

_PROVIDER_ENV_KEYS = (
    "ANTIGONA_PROVIDER",
    "ACTIVE_PROVIDER",
    "PROVIDER_BASE_URL",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTIGONA_MODEL",
    "ANTIGONA_MODEL_PRIMARY",
    "MODEL_PRIMARY",
    "OLLAMA_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_provider_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolate project root and provider state."""
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))
    for key in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    ProviderResolver.clear_cache()
    yield
    ProviderResolver.clear_cache()


def test_runtime_refreshes_model_on_setllm_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario A & C: Model change in /setllm must update the next turn in the same process."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-initial")
    _save_state("deepseek", "deepseek-chat")

    settings = Settings.from_env()
    runtime = build_turn_runtime(
        base_url="https://api.deepseek.com",
        api_key="sk-deepseek-initial",
        model="deepseek-chat",
        workspace_path=str(settings.workspace),
        settings=settings,
    )

    # Initial turn uses deepseek-chat
    assert runtime.worker.engine.provider.config.model == "deepseek-chat"
    assert runtime.worker.engine.provider.config.base_url == "https://api.deepseek.com"

    # Operator runs /setllm deepseek deepseek-reasoner
    _save_state("deepseek", "deepseek-reasoner")

    # Second turn on the SAME runtime instance must use deepseek-reasoner
    with patch.object(
        TurnWorker,
        "execute_task",
        new_callable=AsyncMock,
        return_value=TurnResult(success=True, final_response="done"),
    ):
        runtime.execute("flow-2", "goal", [])

    assert runtime.worker.engine.provider.config.model == "deepseek-reasoner"


def test_runtime_refreshes_provider_on_setllm_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario B & C: Provider switch (DeepSeek -> OpenRouter) must update the next turn."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-key")
    _save_state("deepseek", "deepseek-chat")

    settings = Settings.from_env()
    runtime = build_turn_runtime(
        base_url="https://api.deepseek.com",
        api_key="sk-deepseek-key",
        model="deepseek-chat",
        workspace_path=str(settings.workspace),
        settings=settings,
    )

    assert "deepseek" in runtime.worker.engine.provider.config.base_url
    assert runtime.worker.engine.provider.config.api_key == "sk-deepseek-key"

    # Operator runs /setllm openrouter anthropic/claude-3.5-sonnet
    _save_state("openrouter", "anthropic/claude-3.5-sonnet")

    # Second turn on the SAME runtime instance
    with patch.object(
        TurnWorker,
        "execute_task",
        new_callable=AsyncMock,
        return_value=TurnResult(success=True, final_response="done"),
    ):
        runtime.execute("flow-2", "goal", [])

    assert "openrouter.ai" in runtime.worker.engine.provider.config.base_url
    assert runtime.worker.engine.provider.config.api_key == "sk-openrouter-key"
    assert runtime.worker.engine.provider.config.model == "anthropic/claude-3.5-sonnet"


def test_runtime_fails_closed_when_new_provider_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario E: If operator switches to an uncredentialed provider, fail closed."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key")
    _save_state("deepseek", "deepseek-chat")

    settings = Settings.from_env()
    runtime = build_turn_runtime(
        base_url="https://api.deepseek.com",
        api_key="sk-deepseek-key",
        model="deepseek-chat",
        workspace_path=str(settings.workspace),
        settings=settings,
    )

    # Operator switches to openrouter but NO OPENROUTER_API_KEY is in env
    _save_state("openrouter", "anthropic/claude-3.5-sonnet")

    # Must raise / fail closed, NEVER silently continue on DeepSeek
    with pytest.raises(RuntimeError, match="no LLM provider resolved"):
        runtime.execute("flow-2", "goal", [])


def test_runtime_reuses_worker_when_config_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario F: When config does not change, worker instance is reused."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key")
    _save_state("deepseek", "deepseek-chat")

    settings = Settings.from_env()
    runtime = build_turn_runtime(
        base_url="https://api.deepseek.com",
        api_key="sk-deepseek-key",
        model="deepseek-chat",
        workspace_path=str(settings.workspace),
        settings=settings,
    )

    w1 = runtime.worker
    w2 = runtime.worker
    assert w1 is w2, "TurnWorker instance must be reused across turns when config is unchanged"


def test_current_turn_stability_during_mid_turn_setllm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario D: Mid-flight /setllm does not corrupt the currently executing turn."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-key")
    _save_state("deepseek", "deepseek-chat")

    settings = Settings.from_env()
    runtime = build_turn_runtime(
        base_url="https://api.deepseek.com",
        api_key="sk-deepseek-key",
        model="deepseek-chat",
        workspace_path=str(settings.workspace),
        settings=settings,
    )

    turn1_executing_provider: list[str] = []

    async def _mid_turn_exec(worker_self, flow_id: str, goal: str, messages: list, budget=None):
        # Record what provider config the currently-executing worker instance is using
        turn1_executing_provider.append(worker_self.engine.provider.config.base_url)
        # Simulate operator running /setllm WHILE turn 1 is actively executing
        _save_state("openrouter", "anthropic/claude-3.5-sonnet")
        return TurnResult(success=True, final_response="turn1-done")

    with patch.object(TurnWorker, "execute_task", _mid_turn_exec):
        res1 = runtime.execute("flow-1", "goal-1", [])
        assert res1.success is True

    # Turn 1 ran to completion with DeepSeek
    assert "deepseek" in turn1_executing_provider[0]

    # Turn 2 executed AFTER Turn 1 completes must now see OpenRouter
    with patch.object(
        TurnWorker,
        "execute_task",
        new_callable=AsyncMock,
        return_value=TurnResult(success=True, final_response="turn2-done"),
    ):
        res2 = runtime.execute("flow-2", "goal-2", [])
        assert res2.success is True

    assert "openrouter.ai" in runtime.worker.engine.provider.config.base_url


def test_turn_task_executor_end_to_end_across_setllm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live TurnTaskExecutor execution across multiple tasks with /setllm in between."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "sample.txt").write_text("sample content", encoding="utf-8")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-key")
    _save_state("deepseek", "deepseek-chat")

    settings = Settings.from_env()
    runtime = build_turn_runtime(
        base_url="https://api.deepseek.com",
        api_key="sk-deepseek-key",
        model="deepseek-chat",
        workspace_path=str(ws),
        settings=settings,
    )

    db_url = f"sqlite:///{tmp_path / 'worker_test.sqlite'}"
    db = Database(db_url)
    db.create_all()

    models_used: list[str] = []

    async def _capture_exec(worker_self, flow_id: str, goal: str, messages: list, budget=None):
        models_used.append(worker_self.engine.provider.config.model)
        return TurnResult(success=True, final_response="task result content")

    class FakeVerifier:
        def request_verification(self, *args, **kwargs):
            return "DONE"

    with patch.object(TurnWorker, "execute_task", _capture_exec):
        # Task 1
        with db.session_factory() as session:
            repo = TaskRepository(session)
            t1, _ = repo.create(CreateTask("owner", "read sample.txt", "sample.txt", "", "idem-1", tool_name="workspace.read_text"))
            repo.commit()
            t1_id = t1.id

        with db.session_factory() as session:
            t1 = TaskRepository(session).get(t1_id)
            TurnTaskExecutor(session, FakeVerifier(), runtime=runtime, workspace_root=ws).run(t1, "worker-1")

        assert models_used[0] == "deepseek-chat"

        # Operator changes model via /setllm
        _save_state("openrouter", "meta-llama/llama-3.3-70b-instruct")

        # Task 2
        with db.session_factory() as session:
            repo = TaskRepository(session)
            t2, _ = repo.create(CreateTask("owner", "read sample.txt", "sample.txt", "", "idem-2", tool_name="workspace.read_text"))
            repo.commit()
            t2_id = t2.id

        with db.session_factory() as session:
            t2 = TaskRepository(session).get(t2_id)
            TurnTaskExecutor(session, FakeVerifier(), runtime=runtime, workspace_root=ws).run(t2, "worker-1")

        assert models_used[1] == "meta-llama/llama-3.3-70b-instruct"
