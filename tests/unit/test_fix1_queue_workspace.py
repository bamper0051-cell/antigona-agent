"""FIX-1: Telegram inbound queue contract + task_tag correlation + hard workspace."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from antigona.channels.telegram.bridge import (
    TelegramBridge,
    TurnIdentity,
    ensure_task_tag_in_first_paragraph,
    extract_task_tag,
)
from antigona.channels.telegram.turn_ledger import QueueStatus, TurnLedger
from antigona.worker.tools.common import ToolError
from antigona.workspace import LocalWorkspace


class BlockingGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.release = asyncio.Event()

    async def send_dialogue_turn(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(str(kwargs["turn_id"]))
        await self.release.wait()
        return {"reply": kwargs["text"]}


def _msg(chat_id: int, message_id: int, **kw: Any) -> TurnIdentity:
    return TurnIdentity(chat_id=chat_id, message_id=message_id, **kw)


def _queue_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT queue_id, chat_id, message_id, task_tag, text, "
            "attachment_ids, status, reply_to_message_id "
            "FROM telegram_queue"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_extract_task_tag_pure() -> None:
    assert extract_task_tag("[T002] ping") == "[T002]"
    assert extract_task_tag("do [T021] stuff") == "[T021]"
    assert extract_task_tag("no tag here") is None
    assert extract_task_tag("") is None


def test_ensure_tag_in_first_paragraph() -> None:
    assert ensure_task_tag_in_first_paragraph("[T002] result", "[T002]") == "[T002] result"
    assert ensure_task_tag_in_first_paragraph("done", "[T002]") == "[T002] done"
    assert ensure_task_tag_in_first_paragraph("hi", None) == "hi"
    once = ensure_task_tag_in_first_paragraph("done", "[T002]")
    assert ensure_task_tag_in_first_paragraph(once, "[T002]") == once


async def test_queue_one_running_rest_queued_per_chat(tmp_path: Path) -> None:
    db = tmp_path / "turns.db"
    ledger = TurnLedger(db)
    gateway = BlockingGateway()
    bridge = TelegramBridge(gateway, ledger=ledger)

    t1 = asyncio.create_task(bridge.turn(identity=_msg(7, 1), text="[T002] a", user_id=1))
    t2 = asyncio.create_task(bridge.turn(identity=_msg(7, 2), text="[T007] b", user_id=1))
    t3 = asyncio.create_task(bridge.turn(identity=_msg(7, 3), text="[T021] c", user_id=1))

    deadline = time.time() + 2.0
    statuses: list[dict[str, Any]] = []
    while time.time() < deadline:
        statuses = _queue_rows(db)
        if any(r["status"] == QueueStatus.RUNNING.value for r in statuses):
            break
        await asyncio.sleep(0.05)

    running = [r for r in statuses if r["status"] == QueueStatus.RUNNING.value]
    queued = [r for r in statuses if r["status"] == QueueStatus.QUEUED.value]
    assert len(running) == 1, statuses
    assert len(queued) == 2, statuses
    for r in statuses:
        assert r["chat_id"] == 7
        assert r["reply_to_message_id"] == r["message_id"]
    tags = {r["message_id"]: r["task_tag"] for r in statuses}
    assert tags == {1: "[T002]", 2: "[T007]", 3: "[T021]"}

    gateway.release.set()
    await asyncio.gather(t1, t2, t3)
    await asyncio.sleep(0.05)
    final = _queue_rows(db)
    assert all(r["status"] == QueueStatus.DONE.value for r in final), final
    await bridge.close()


async def test_queue_duplicate_message_id_dropped(tmp_path: Path) -> None:
    db = tmp_path / "turns.db"
    ledger = TurnLedger(db)
    gateway = BlockingGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway, ledger=ledger)

    ident = _msg(9, 42)
    first = await bridge.turn(identity=ident, text="[T002] hello", user_id=1)
    assert first.replayed is False
    second = await bridge.turn(identity=ident, text="[T002] hello", user_id=1)
    assert second.replayed is True
    assert gateway.calls == [ident.key], gateway.calls

    rows = _queue_rows(db)
    # first execution settled to done; the redelivered copy is dropped, never re-run
    assert {r["status"] for r in rows} == {
        QueueStatus.DONE.value,
        QueueStatus.DROPPED_DUPLICATE.value,
    }, rows
    await bridge.close()


def test_workspace_write_inside_passes(tmp_path: Path) -> None:
    ws = LocalWorkspace(root_path=str(tmp_path / "ws"))
    res = ws.write_file("t002_ping.txt", "ping")
    assert res.sha256 == "758d61f26a44448384e5c4468a0dcb7a2abe456067b0f7b505bc28b9411fe931"
    assert (tmp_path / "ws" / "t002_ping.txt").read_text(encoding="utf-8") == "ping"


def test_workspace_write_outside_refuses(tmp_path: Path) -> None:
    ws = LocalWorkspace(root_path=str(tmp_path / "ws"))
    # escape vectors: traversal into a sibling dir, traversal to the real user
    # home, and an absolute path. The home vector is derived (not a hardcoded
    # ``/home/<name>/`` literal) so it stays portable across hosts.
    home_escape = "/".join(["..", "..", "..", Path.home().name, "x.txt"])
    for bad in ["../../Downloads/x.txt", home_escape, "/tmp/x.txt"]:
        with pytest.raises(ToolError):
            ws.write_file(bad, "nope")
    assert not (Path.home() / "Downloads" / "x.txt").exists()


@pytest.mark.asyncio
async def test_document_ops_and_filesystem_write_boundary(tmp_path: Path) -> None:
    from antigona.tools.contracts import ToolInput
    from antigona.tools.document_ops import DocumentVerificationError, create_and_verify_document
    from antigona.tools.filesystem_write import FilesystemWriteTool

    ws_root = tmp_path / "ws_guard_test"
    ws_root.mkdir(parents=True, exist_ok=True)

    # 1. document_ops boundary
    for bad in ["../../evil.txt", "../other.txt"]:
        with pytest.raises(DocumentVerificationError):
            create_and_verify_document(bad, "payload", workspace_root=ws_root)

    # 2. filesystem_write boundary
    fs_tool = FilesystemWriteTool(root_boundary=str(ws_root))
    for bad in ["../../escape.txt", "/tmp/escape.txt"]:
        result = await fs_tool.execute(
                ToolInput(
                    tool_name="filesystem.write",
                    params={"action": "create_file", "path": bad, "content": "leak"},
                )
            )
        assert result.success is False


async def test_restart_recovery_drains_pending(tmp_path: Path) -> None:
    db = tmp_path / "restart_turns.db"
    ledger1 = TurnLedger(db, owner_token="proc-1")
    gateway1 = BlockingGateway()
    bridge1 = TelegramBridge(gateway1, ledger=ledger1)

    # Enqueue 3 messages; t1 runs and blocks, t2 and t3 stay queued
    t1 = asyncio.create_task(bridge1.turn(identity=_msg(10, 1), text="[T001] first", user_id=1))
    t2 = asyncio.create_task(bridge1.turn(identity=_msg(10, 2), text="[T002] second", user_id=1))
    t3 = asyncio.create_task(bridge1.turn(identity=_msg(10, 3), text="[T003] third", user_id=1))

    # Wait for t1 to start running in gateway
    for _ in range(50):
        if len(gateway1.calls) == 1:
            break
        await asyncio.sleep(0.02)
    assert len(gateway1.calls) == 1

    # Simulate process crash: close bridge1 without releasing gateway
    t1.cancel()
    t2.cancel()
    t3.cancel()
    await bridge1.close()

    # Start new process/bridge
    ledger2 = TurnLedger(db, owner_token="proc-2")
    gateway2 = BlockingGateway()
    gateway2.release.set()
    bridge2 = TelegramBridge(gateway2, ledger=ledger2)

    # Recovery hook
    recovered = await bridge2.recover_on_startup()
    assert recovered >= 1

    rows = _queue_rows(db)
    assert len(rows) == 3
    await bridge2.close()


async def test_12_message_sequential_matrix(tmp_path: Path) -> None:
    db = tmp_path / "matrix_turns.db"
    ledger = TurnLedger(db)
    gateway = BlockingGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway, ledger=ledger)

    inputs = [
        (1, "[T002] create file"),
        (2, "[T007] long text"),
        (3, "[T021] return artifact"),
        (4, "[T029] folder structure"),
        (5, "[T039] edit square"),
        (6, "[T002] repeat command"),
        (7, "[ESC] escape attempt"),
        (8, "[S01] series 1"),
        (9, "[S02] series 2"),
        (10, "[S03] series 3"),
        (11, "[S04] series 4"),
        (12, "[S05] series 5"),
    ]

    for msg_id, text in inputs:
        res = await bridge.turn(identity=_msg(42, msg_id), text=text, user_id=1)
        assert res.payload["reply"] == text

    rows = _queue_rows(db)
    assert len(rows) == 12
    assert all(r["status"] == QueueStatus.DONE.value for r in rows)
    assert [r["message_id"] for r in rows] == list(range(1, 13))

    await bridge.close()

