"""RED probes for Telegram Bridge v1 defects (scratch; superseded by the v1 suite)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from antigona.channels.telegram.bridge import TelegramBridge, TurnIdentity
from antigona.channels.telegram.turn_ledger import TurnLedger


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


async def test_red_enqueue_during_drain_exit_loses_the_turn() -> None:
    gateway = BlockingGateway()
    bridge = TelegramBridge(gateway)
    first = asyncio.create_task(
        bridge.turn(identity=_msg(1, 1), text="a", user_id=1)
    )
    while not gateway.calls:
        await asyncio.sleep(0)

    await bridge._lock.acquire()
    second = asyncio.create_task(
        bridge.turn(identity=_msg(1, 2), text="b", user_id=1)
    )
    await asyncio.sleep(0)
    gateway.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    bridge._lock.release()

    await asyncio.wait_for(first, timeout=1.0)
    await asyncio.wait_for(second, timeout=1.0)
    assert gateway.calls == ["telegram:1:-:message:1:0", "telegram:1:-:message:2:0"]
    await bridge.close()


async def test_red_edit_of_same_message_id_is_not_a_duplicate() -> None:
    gateway = BlockingGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway)
    await bridge.turn(identity=_msg(5, 100), text="original", user_id=1)
    await bridge.turn(
        identity=_msg(5, 100, kind="edited", revision=1700000000),
        text="[правка] edited",
        user_id=1,
    )
    assert len(gateway.calls) == 2, gateway.calls
    await bridge.close()


async def test_red_durable_ledger_survives_process_restart(tmp_path: Path) -> None:
    ledger_path = tmp_path / "turns.db"
    identity = _msg(9, 42)

    gateway = BlockingGateway()
    gateway.release.set()
    first = TelegramBridge(gateway, ledger=TurnLedger(ledger_path))
    result = await first.turn(identity=identity, text="hello", user_id=1)
    assert result.payload["reply"] == "hello"
    await first.close()

    restarted_gateway = BlockingGateway()
    restarted_gateway.release.set()
    second = TelegramBridge(restarted_gateway, ledger=TurnLedger(ledger_path))
    replay = await second.turn(identity=identity, text="hello", user_id=1)
    assert replay.replayed is True
    assert replay.payload["reply"] == "hello"
    assert restarted_gateway.calls == []
    await second.close()


async def test_red_session_count_is_globally_bounded() -> None:
    gateway = BlockingGateway()
    bridge = TelegramBridge(gateway, max_queue_per_session=1)
    tasks = [
        asyncio.create_task(bridge.turn(identity=_msg(c, 1), text="x", user_id=1))
        for c in range(500)
    ]
    await asyncio.sleep(0.05)
    assert bridge.session_count <= 64, bridge.session_count
    gateway.release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bridge.close()
