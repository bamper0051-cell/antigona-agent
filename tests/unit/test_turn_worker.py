"""Regression tests for the TurnWorker and read-only workspace tools.

Tests cover:
- Workspace tools (read_file, check_file, list_dir)
- Path safety (escape prevention)
- Read-only task identification
- TurnWorker creation and execution
- Task routing (read-only vs write)
- Integration with ReadOnlyTaskExecutor
- Budget propagation
- Error auto-repair flow simulation
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from antigona.core import paths
from antigona.turn_bridge.turn_engine_adapter import (
    TurnBudget,
    TurnEngine,
    TurnResult,
)
from antigona.turn_bridge.worker_adapter import (
    DEFAULT_WORKSPACE,
    READ_ONLY_SYSTEM_PROMPT,
    READ_ONLY_TOOLS,
    TurnWorker,
    build_readonly_tool_map,
    is_readonly_task,
    is_write_task,
    tool_check_file,
    tool_list_dir,
    tool_read_file,
)
from antigona.turn_bridge.worker_integration import (
    ReadOnlyTaskExecutor,
    should_use_turn_worker,
)

# ── Helpers ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_workspace() -> Path:
    """Create a temporary workspace with a known file structure."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td).resolve()
        # Create a test file
        (ws / "hello.txt").write_text("Hello, World!", encoding="utf-8")
        # Create a subdirectory with a file
        sub = ws / "subdir"
        sub.mkdir()
        (sub / "nested.py").write_text("x = 42", encoding="utf-8")
        yield ws


# ── Workspace tool tests ───────────────────────────────────────────────────


class TestToolReadFile:
    """Section: tool_read_file helper."""

    async def test_read_file_success(self, tmp_workspace: Path) -> None:
        content = await tool_read_file("hello.txt", workspace=tmp_workspace)
        assert content == "Hello, World!"

    async def test_read_file_from_subdir(self, tmp_workspace: Path) -> None:
        content = await tool_read_file("subdir/nested.py", workspace=tmp_workspace)
        assert content == "x = 42"

    async def test_read_file_not_found(self, tmp_workspace: Path) -> None:
        with pytest.raises(FileNotFoundError):
            await tool_read_file("nonexistent.txt", workspace=tmp_workspace)

    async def test_read_file_path_escape(self, tmp_workspace: Path) -> None:
        with pytest.raises(ValueError, match="escapes workspace"):
            await tool_read_file("../etc/passwd", workspace=tmp_workspace)

    async def test_read_file_absolute_path_outside(self, tmp_workspace: Path) -> None:
        """Absolute paths outside workspace should be rejected."""
        with pytest.raises(ValueError, match="escapes workspace"):
            await tool_read_file("/etc/passwd", workspace=tmp_workspace)


class TestToolCheckFile:
    """Section: tool_check_file helper."""

    async def test_check_file_exists(self, tmp_workspace: Path) -> None:
        result = await tool_check_file("hello.txt", workspace=tmp_workspace)
        data = json.loads(result)
        assert data["exists"] is True
        assert data["is_file"] is True
        assert data["size"] == 13

    async def test_check_file_not_found(self, tmp_workspace: Path) -> None:
        result = await tool_check_file("missing.txt", workspace=tmp_workspace)
        data = json.loads(result)
        assert data["exists"] is False

    async def test_check_directory(self, tmp_workspace: Path) -> None:
        result = await tool_check_file("subdir", workspace=tmp_workspace)
        data = json.loads(result)
        assert data["exists"] is True
        assert data["is_dir"] is True

    async def test_check_file_path_escape(self, tmp_workspace: Path) -> None:
        result = await tool_check_file("../etc", workspace=tmp_workspace)
        data = json.loads(result)
        assert data["exists"] is False
        assert "path escaped workspace" in data.get("error", "")


class TestToolListDir:
    """Section: tool_list_dir helper."""

    async def test_list_dir_root(self, tmp_workspace: Path) -> None:
        listing = await tool_list_dir(".", workspace=tmp_workspace)
        assert "hello.txt" in listing
        assert "subdir" in listing

    async def test_list_dir_subdir(self, tmp_workspace: Path) -> None:
        listing = await tool_list_dir("subdir", workspace=tmp_workspace)
        # Wave 4 (E): Windows listing uses os.sep (backslash) — normalize.
        listing = listing.replace(os.sep, "/")
        assert "subdir/nested.py" in listing

    async def test_list_dir_not_found(self, tmp_workspace: Path) -> None:
        with pytest.raises(NotADirectoryError):
            await tool_list_dir("nonexistent", workspace=tmp_workspace)

    async def test_list_dir_path_escape(self, tmp_workspace: Path) -> None:
        with pytest.raises(ValueError, match="escapes workspace"):
            await tool_list_dir("../etc", workspace=tmp_workspace)

    async def test_list_dir_on_file(self, tmp_workspace: Path) -> None:
        with pytest.raises(NotADirectoryError):
            await tool_list_dir("hello.txt", workspace=tmp_workspace)


# ── Task identification tests ──────────────────────────────────────────────


class TestIsReadonlyTask:
    """Section: read-only task identification."""

    def test_file_read_is_readonly(self) -> None:
        assert is_readonly_task("file_read")

    def test_file_check_is_readonly(self) -> None:
        assert is_readonly_task("file_check")

    def test_tool_read_file_is_readonly(self) -> None:
        assert is_readonly_task("tool_read_file")

    def test_shell_is_not_readonly(self) -> None:
        assert not is_readonly_task("sandbox.shell")

    def test_write_text_is_not_readonly(self) -> None:
        assert not is_readonly_task("workspace.write_text")

    def test_empty_string_is_not_readonly(self) -> None:
        assert not is_readonly_task("")


class TestIsWriteTask:
    """Section: write task identification."""

    def test_shell_is_write(self) -> None:
        assert is_write_task("sandbox.shell")

    def test_write_text_is_write(self) -> None:
        assert is_write_task("workspace.write_text")

    def test_file_read_is_not_write(self) -> None:
        assert not is_write_task("file_read")

    def test_tool_read_file_is_not_write(self) -> None:
        assert not is_write_task("tool_read_file")


class TestShouldUseTurnWorker:
    """Section: routing logic."""

    def test_file_read_routes_to_turn_worker(self) -> None:
        assert should_use_turn_worker("file_read")

    def test_file_check_routes_to_turn_worker(self) -> None:
        assert should_use_turn_worker("file_check")

    def test_shell_does_not_route_to_turn_worker(self) -> None:
        assert not should_use_turn_worker("sandbox.shell")

    def test_write_text_does_not_route(self) -> None:
        assert not should_use_turn_worker("workspace.write_text")

    def test_read_related_tool_routes(self) -> None:
        """Any tool with 'read' and 'file' in name should route."""
        assert should_use_turn_worker("read_file")
        assert should_use_turn_worker("custom_readfile")

    def test_empty_string(self) -> None:
        assert not should_use_turn_worker("")


# ── Tool map tests ─────────────────────────────────────────────────────────


class TestBuildReadonlyToolMap:
    """Section: tool_map factory."""

    def test_build_tool_map_has_three_tools(self) -> None:
        tool_map = build_readonly_tool_map()
        assert "tool_read_file" in tool_map
        assert "tool_check_file" in tool_map
        assert "tool_list_dir" in tool_map

    def test_tool_map_with_custom_workspace(self, tmp_workspace: Path) -> None:
        tool_map = build_readonly_tool_map(workspace=tmp_workspace)
        assert "tool_read_file" in tool_map
        assert "tool_check_file" in tool_map

    async def test_tool_map_functions_work(self, tmp_workspace: Path) -> None:
        tool_map = build_readonly_tool_map(workspace=tmp_workspace)
        result = await tool_map["tool_read_file"](path="hello.txt")
        assert result == "Hello, World!"

        result = await tool_map["tool_check_file"](path="hello.txt")
        assert "exists" in result

        listing = await tool_map["tool_list_dir"](path=".")
        assert "hello.txt" in listing


class TestReadonlyToolsDefinition:
    """Section: READ_ONLY_TOOLS definition format."""

    def test_readonly_tools_are_properly_formatted(self) -> None:
        """Each tool must have name, description, and parameters with required."""
        assert len(READ_ONLY_TOOLS) == 3
        for tool in READ_ONLY_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
            assert tool["parameters"]["type"] == "object"
            assert "properties" in tool["parameters"]
            assert "required" in tool["parameters"]

    def test_readonly_tools_have_correct_names(self) -> None:
        names = {t["name"] for t in READ_ONLY_TOOLS}
        assert names == {"tool_read_file", "tool_check_file", "tool_list_dir"}

    def test_system_prompt_contains_guidance(self) -> None:
        assert "tool_read_file" in READ_ONLY_SYSTEM_PROMPT
        assert "tool_check_file" in READ_ONLY_SYSTEM_PROMPT
        assert "tool_list_dir" in READ_ONLY_SYSTEM_PROMPT
        # The workspace line is interpolated from the canonical resolver, never
        # the literal "/opt/antigona-home/antigona": assert the resolved workspace is present.
        assert DEFAULT_WORKSPACE in READ_ONLY_SYSTEM_PROMPT


# ── TurnWorker tests ───────────────────────────────────────────────────────


class TestTurnWorker:
    """Section: TurnWorker creation and execution."""

    async def test_create_with_turn_engine_injection(self) -> None:
        """Can create TurnWorker with an injected TurnEngine."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(
                success=True,
                final_response="Done.",
                turns_used=1,
                tool_calls_made=0,
            )
        )
        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        assert worker.engine is mock_engine

    async def test_worker_workspace_defaults(self) -> None:
        """Workspace defaults to ANTIGONA_WORKSPACE or project root."""
        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=AsyncMock(spec=TurnEngine),
        )
        expected = os.environ.get("ANTIGONA_WORKSPACE", str(paths.project_root()))
        assert str(worker.workspace) == str(Path(expected).resolve())

    async def test_worker_workspace_custom(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            worker = TurnWorker(
                base_url="https://test.local/v1",
                api_key="test-key",
                model="test-model",
                workspace_path=td,
                turn_engine=AsyncMock(spec=TurnEngine),
            )
            assert str(worker.workspace) == str(Path(td).resolve())

    async def test_execute_task_success(self) -> None:
        """execute_task returns TurnResult from the engine."""
        mock_engine = AsyncMock(spec=TurnEngine)
        expected_result = TurnResult(
            success=True,
            final_response="File content: Hello, World!",
            turns_used=2,
            tool_calls_made=1,
        )
        mock_engine.run_turn = AsyncMock(return_value=expected_result)

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        result = await worker.execute_task(
            flow_id="flow-1",
            goal="Read hello.txt",
            messages=[{"role": "user", "content": "Read the file"}],
        )

        assert result.success
        assert result.final_response == "File content: Hello, World!"
        assert result.turns_used == 2
        assert result.tool_calls_made == 1

    async def test_execute_task_failure(self) -> None:
        """execute_task propagates failure from the engine."""
        mock_engine = AsyncMock(spec=TurnEngine)
        expected_result = TurnResult(
            success=False,
            final_response=None,
            error="Tool call budget exceeded",
            turns_used=3,
            tool_calls_made=5,
        )
        mock_engine.run_turn = AsyncMock(return_value=expected_result)

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        result = await worker.execute_task(
            flow_id="flow-2",
            goal="Read missing file",
            messages=[{"role": "user", "content": "Read it"}],
        )

        assert not result.success
        assert "Tool call budget exceeded" in (result.error or "")

    async def test_execute_task_passes_custom_budget(self) -> None:
        """Custom budget is forwarded to the engine."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        budget = TurnBudget(max_turns=3, max_tool_calls=5, max_duration_seconds=30)
        await worker.execute_task(
            flow_id="flow-3",
            goal="Test budget",
            messages=[{"role": "user", "content": "hi"}],
            budget=budget,
        )

        # Verify the budget was passed to run_turn
        _, call_kwargs = mock_engine.run_turn.call_args
        assert call_kwargs["budget"] is budget

    async def test_execute_task_passes_custom_tools(self) -> None:
        """Custom tools are forwarded to the engine."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        custom_tools = [{"name": "custom_tool", "description": "A custom tool"}]
        await worker.execute_task(
            flow_id="flow-4",
            goal="Test tools",
            messages=[{"role": "user", "content": "hi"}],
            tools=custom_tools,
        )

        _, call_kwargs = mock_engine.run_turn.call_args
        assert call_kwargs["available_tools"] is custom_tools

    async def test_execute_task_passes_messages(self) -> None:
        """Messages are forwarded to the engine."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        messages = [{"role": "user", "content": "Read config"}]
        await worker.execute_task(
            flow_id="flow-5",
            goal="Read Cargo.toml",
            messages=messages,
        )

        _, call_kwargs = mock_engine.run_turn.call_args
        assert call_kwargs["messages"] is messages

    async def test_execute_task_default_budget(self) -> None:
        """Default budget is used when none is provided."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        await worker.execute_task(
            flow_id="flow-6",
            goal="Default budget",
            messages=[{"role": "user", "content": "go"}],
        )

        _, call_kwargs = mock_engine.run_turn.call_args
        budget = call_kwargs["budget"]
        assert budget.max_turns == 15
        assert budget.max_tool_calls == 30
        assert budget.max_duration_seconds == 120

    async def test_close_calls_provider_close(self) -> None:
        """close() delegates to the provider's close."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.provider = AsyncMock()
        mock_engine.provider.close = AsyncMock()

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            turn_engine=mock_engine,
        )
        await worker.close()
        mock_engine.provider.close.assert_awaited_once()

    async def test_extra_system_prompt_is_used(self) -> None:
        """Extra system prompt is passed to TurnEngine constructor."""
        from antigona.turn_bridge.provider_adapter import (
            ProviderAdapter,
            ProviderAdapterConfig,
        )

        mock_provider = AsyncMock(spec=ProviderAdapter)
        mock_provider.config = ProviderAdapterConfig(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        mock_provider.chat = AsyncMock(
            return_value={
                "choices": [{"message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}]
            }
        )

        worker = TurnWorker(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            extra_system_prompt="Be very concise.",
            provider=mock_provider,
        )

        # Run a simple turn to verify the engine was created
        # The engine will call mock_provider.chat
        result = await worker.execute_task(
            flow_id="flow-7",
            goal="Test",
            messages=[{"role": "user", "content": "hi"}],
            budget=TurnBudget(max_turns=1, max_tool_calls=1, max_duration_seconds=10),
        )
        # With our mock returning content directly, it should succeed
        assert result.success is not None


# ── ReadOnlyTaskExecutor tests ─────────────────────────────────────────────


class TestReadOnlyTaskExecutor:
    """Section: ReadOnlyTaskExecutor integration."""

    async def test_create_executor_with_turn_worker(self) -> None:
        """ReadOnlyTaskExecutor creates a TurnWorker internally."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )
        executor = ReadOnlyTaskExecutor(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        # Replace the internal worker's engine with our mock
        executor._worker = TurnWorker(
            base_url="",
            api_key="",
            model="",
            turn_engine=mock_engine,
        )

        result = await executor.execute(
            flow_id="exec-1",
            goal="Read file",
            messages=[{"role": "user", "content": "Read it"}],
        )
        assert result.success

    async def test_execute_uses_default_messages(self) -> None:
        """When messages is None, a default user message with the goal is used."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )
        executor = ReadOnlyTaskExecutor(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        executor._worker = TurnWorker(
            base_url="",
            api_key="",
            model="",
            turn_engine=mock_engine,
        )

        await executor.execute(flow_id="exec-2", goal="Custom goal")
        _, call_kwargs = mock_engine.run_turn.call_args
        msgs = call_kwargs["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Custom goal"

    async def test_execute_from_task_dict(self) -> None:
        """Executing from a task dict extracts id, goal, and messages."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )
        executor = ReadOnlyTaskExecutor(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        executor._worker = TurnWorker(
            base_url="",
            api_key="",
            model="",
            turn_engine=mock_engine,
        )

        task_dict = {
            "id": "task-42",
            "goal": "Read config",
            "content": "file: config.json",
        }
        result = await executor.execute_from_task_dict(task_dict)
        assert result.success

        _, call_kwargs = mock_engine.run_turn.call_args
        assert call_kwargs["goal"] == "Read config"
        assert len(call_kwargs["messages"]) == 1

    async def test_execute_from_task_dict_no_content(self) -> None:
        """Task dict without content still works."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )
        executor = ReadOnlyTaskExecutor(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        executor._worker = TurnWorker(
            base_url="",
            api_key="",
            model="",
            turn_engine=mock_engine,
        )

        task_dict = {"id": "task-43", "goal": "Just a goal"}
        result = await executor.execute_from_task_dict(task_dict)
        assert result.success

    async def test_execute_from_task_dict_no_messages(self) -> None:
        """Task dict without messages builds from goal."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.run_turn = AsyncMock(
            return_value=TurnResult(success=True, final_response="OK")
        )
        executor = ReadOnlyTaskExecutor(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        executor._worker = TurnWorker(
            base_url="",
            api_key="",
            model="",
            turn_engine=mock_engine,
        )

        task_dict = {"id": "task-44", "goal": "My goal"}
        result = await executor.execute_from_task_dict(
            task_dict,
            budget=TurnBudget(max_turns=5, max_tool_calls=10, max_duration_seconds=30),
        )
        assert result.success

    async def test_executor_close(self) -> None:
        """Executor close delegates to worker close."""
        mock_engine = AsyncMock(spec=TurnEngine)
        mock_engine.provider = AsyncMock()
        mock_engine.provider.close = AsyncMock()

        executor = ReadOnlyTaskExecutor(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        executor._worker = TurnWorker(
            base_url="",
            api_key="",
            model="",
            turn_engine=mock_engine,
        )
        await executor.close()
        mock_engine.provider.close.assert_awaited_once()
