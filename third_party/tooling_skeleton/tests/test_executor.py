import asyncio
from pathlib import Path

from antigona.tools import ToolCall, ToolContext, ToolExecutor, ToolStatus, build_default_registry


def test_read_file_through_executor(tmp_path):
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    executor = ToolExecutor(build_default_registry())
    call = ToolCall(
        "read_file",
        {"path": "hello.txt"},
        "File should contain greeting",
        "read_file returns exact text",
    )
    result = asyncio.run(executor.execute(call, ToolContext("op", tmp_path)))
    assert result.status is ToolStatus.SUCCESS
    assert result.stdout == "hello"


def test_path_escape_is_normalized_to_error(tmp_path):
    executor = ToolExecutor(build_default_registry())
    call = ToolCall(
        "read_file",
        {"path": "../secret.txt"},
        "Check boundary",
        "Attempt must be rejected",
    )
    result = asyncio.run(executor.execute(call, ToolContext("op", tmp_path)))
    assert result.status is ToolStatus.ERROR
    assert result.error_type == "PermissionError"
