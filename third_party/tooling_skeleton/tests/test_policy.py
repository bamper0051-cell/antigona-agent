from pathlib import Path

from antigona.tools import ToolCall, ToolCallRecord, ToolContext, ToolPolicyEngine, ToolResult, ToolStatus, build_default_registry


def test_duplicate_call_is_denied():
    registry = build_default_registry()
    spec = registry.require("read_file")
    call = ToolCall("read_file", {"path": "README.md"}, "Inspect docs", "Need exact content")
    result = ToolResult(call.call_id, call.tool_name, ToolStatus.SUCCESS, "ok")
    decision = ToolPolicyEngine().authorize(
        spec,
        call,
        ToolContext("op", Path.cwd()),
        [ToolCallRecord(call, result)],
    )
    assert not decision.allowed
    assert decision.code == "SEMANTIC_DUPLICATE"


def test_missing_hypothesis_is_denied():
    registry = build_default_registry()
    spec = registry.require("read_file")
    call = ToolCall("read_file", {"path": "README.md"}, "", "Need content")
    decision = ToolPolicyEngine().authorize(spec, call, ToolContext("op", Path.cwd()), [])
    assert decision.code == "MISSING_HYPOTHESIS"
