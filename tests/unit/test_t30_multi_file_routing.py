"""T30 multi-file routing regression tests."""

from __future__ import annotations

from typing import Any

import pytest

from antigona.core.brain import AntigonaBrain, ResponseType
from antigona.router.intent_router import IntentRouter
from antigona.task_goal import parse_goal

T30 = (
    "Создай папку antigona_manual_test. В ней создай три файла: one.txt, "
    "two.txt, three.txt со значениями ONE, TWO, THREE. Затем прочитай все "
    "три. Создай summary.txt со строками <filename>: <content>. Прочитай "
    "summary и покажи полный результат."
)


class _RecordingBackend:
    def __init__(self) -> None:
        self.submits: list[dict[str, Any]] = []

    async def submit_task(self, **kwargs: Any) -> dict[str, Any]:
        self.submits.append(dict(kwargs))
        return {"flow_id": "flow-t30", "id": "flow-t30", "status": "QUEUED"}

    async def cancel_flow(self, flow_id: str) -> None:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED", "artifacts": []})()

    async def steer_flow(self, flow_id: str, message: str) -> None:
        return None


class _NoDraftEngine:
    async def draft_file_content_result(self, text: str, session_id: str) -> Any:
        raise AssertionError("T30 multi_file must use parse_goal plan, not LLM file drafting")

    async def close(self) -> None:
        return None


def test_t30_router_prefers_multi_file_over_read_request() -> None:
    plan = parse_goal(T30)
    assert plan.intent == "multi_file"

    decision = IntentRouter().route(T30)

    assert decision.intent == "task.multi_file"
    assert decision.intent != "task.file_read"
    assert decision.entities["path"] == plan.path
    assert decision.entities["content"] == plan.content
    assert decision.entities["command"] == plan.command
    assert decision.reason_code == "multi_file_goal_parser"


@pytest.mark.asyncio
async def test_t30_brain_submits_multi_file_shell_plan_not_read_text() -> None:
    plan = parse_goal(T30)
    backend = _RecordingBackend()
    brain = AntigonaBrain(dialogue_engine=_NoDraftEngine(), task_backend=backend)

    response = await brain.process(
        T30,
        user_id="owner",
        channel="cli",
        session_id="t30-session",
    )

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    submit = backend.submits[0]
    assert submit["tool_name"] == "sandbox.shell"
    assert submit["tool_name"] != "workspace.read_text"
    assert submit["path"] == plan.path
    assert submit["content"] == plan.content
    assert submit["command"] == (plan.command,)
    assert "printf 'ONE\\n' > antigona_manual_test/one.txt" in submit["command"][0]
    assert "cat antigona_manual_test/summary.txt" in submit["command"][0]
