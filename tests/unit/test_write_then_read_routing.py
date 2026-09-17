"""Write-then-read routing regression: compound "create file with content, then read it back".

Defect (Antigona exam): the request «Создай exam.txt с одной строкой: X. Потом
прочитай его» was routed READ-first (task.file_read), so workspace.read_text was
submitted for a file that did not exist yet -> FAILED "not a file" and the write
was never performed. Rule: file_write_read goals must be WRITE-first so step 0 =
workspace.write_text, step 1 = workspace.read_text of the same file.
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.core.brain import AntigonaBrain, ResponseType
from antigona.router.intent_router import IntentRouter
from antigona.task_goal import parse_goal

WRITE_READ = "Создай exam_fix.txt с одной строкой: ANTIGONA FIX OK. Потом прочитай его"

WRITE_READ_CLASSIC = (
    "Создай exam_42.txt с содержимым ANTIGONA EXAM 42. "
    "Потом прочитай его и пришли содержимое"
)


class _RecordingBackend:
    def __init__(self) -> None:
        self.submits: list[dict[str, Any]] = []

    async def submit_task(self, **kwargs: Any) -> dict[str, Any]:
        self.submits.append(dict(kwargs))
        return {"flow_id": "flow-wr", "id": "flow-wr", "status": "QUEUED"}

    async def cancel_flow(self, flow_id: str) -> None:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED", "artifacts": []})()

    async def steer_flow(self, flow_id: str, message: str) -> None:
        return None


class _NoDraft:
    async def draft_file_content_result(self, text: str, session_id: str) -> Any:
        raise AssertionError(
            "write_then_read must use parse_goal content, not LLM file drafting"
        )

    async def close(self) -> None:
        return None


def test_write_then_read_router_prefers_write_over_read_heuristic() -> None:
    """Compound create+read must route to task.file_write, never task.file_read."""
    plan = parse_goal(WRITE_READ)
    assert plan.intent == "file_write_read"
    assert plan.read_after_write is True

    decision = IntentRouter().route(WRITE_READ)

    assert decision.intent == "task.file_write"
    assert decision.intent != "task.file_read"
    assert decision.reason_code == "write_then_read_goal_parser"
    assert decision.entities["path"] == "exam_fix.txt"
    assert decision.entities["content"] == "ANTIGONA FIX OK"
    assert decision.entities.get("read_after_write") is True


def test_write_then_read_path_and_content_are_real() -> None:
    plan = parse_goal(WRITE_READ_CLASSIC)
    assert plan.intent == "file_write_read"
    assert plan.path == "exam_42.txt"
    assert plan.content == "ANTIGONA EXAM 42"
    assert plan.read_after_write is True


@pytest.mark.asyncio
async def test_write_then_read_brain_submits_ordered_write_read() -> None:
    """Brain must submit tool=write_text (write step 0) with exact content and
    read_after_write=True (so a read_text step 1 is created), not a read-first plan."""
    backend = _RecordingBackend()
    brain = AntigonaBrain(dialogue_engine=_NoDraft(), task_backend=backend)

    response = await brain.process(
        WRITE_READ,
        user_id="owner",
        channel="cli",
        session_id="wr-session",
    )

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    submit = backend.submits[0]
    # tool_name None -> task_backend defaults to workspace.write_text (write step 0)
    assert submit["tool_name"] in (None, "workspace.write_text")
    assert submit["tool_name"] != "workspace.read_text"
    assert submit["path"] == "exam_fix.txt"
    assert submit["content"] == "ANTIGONA FIX OK"
    assert submit["read_after_write"] is True


def test_single_word_content_not_mangled_by_line_descriptor_strip() -> None:
    """The line-count descriptor strip must not eat a content token."""
    plan = parse_goal("Создай файл hello.txt с текстом Hello World")
    assert plan.content == "Hello World"
