"""Telegram Bridge v1 — hermetic orchestration, attachment and format probes.

Scope: the bridge as the single Telegram orchestration owner.  Composition with
the real handler/presenter lives in ``test_telegram_bridge_production.py``.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from antigona.channels.telegram.bridge import (
    AmbiguousTurn,
    AttachmentRejected,
    BridgeCapacityExceeded,
    BridgeOverflow,
    TelegramBridge,
    TurnCancelled,
    TurnIdentity,
    discard_inbound_file,
    finalize_inbound_file,
    prepare_inbound_file,
    prepare_inbound_path,
    resolve_outbound_artifact,
    sanitize_filename,
    split_telegram_html,
    split_telegram_text,
    utf16_length,
)
from antigona.channels.telegram.turn_ledger import TurnLedger, TurnState


class FakeGateway:
    """Counts invocations and can hold every turn open on demand."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.texts: list[str] = []
        self.active = 0
        self.max_active = 0
        self.release = asyncio.Event()

    async def send_dialogue_turn(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(str(kwargs["turn_id"]))
        self.texts.append(str(kwargs["text"]))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await self.release.wait()
        self.active -= 1
        return {"reply": kwargs["text"], "response_type": "conversation"}


def msg(chat_id: int, message_id: int, **kw: Any) -> TurnIdentity:
    return TurnIdentity(chat_id=chat_id, message_id=message_id, **kw)


async def until_active(gateway: FakeGateway, count: int) -> None:
    """Wait until ``count`` turns have actually entered the gateway.

    Accepting a turn now includes a durable ledger claim, so "one event-loop
    tick" is no longer a reliable proxy for "the worker picked it up".
    """
    for _ in range(2000):
        if len(gateway.calls) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"gateway never reached {count} active turns (saw {len(gateway.calls)})"
    )


async def settle(ticks: int = 50) -> None:
    """Let already-scheduled bridge work reach a steady state."""
    for _ in range(ticks):
        await asyncio.sleep(0)


# ── FIFO, exactly-once, retry safety ─────────────────────────────────────────


async def test_fifo_exactly_once_and_retry_does_not_rerun() -> None:
    gateway = FakeGateway()
    bridge = TelegramBridge(gateway, max_queue_per_session=3)
    first = asyncio.create_task(
        bridge.turn(identity=msg(7, 10), text="one", user_id=1)
    )
    await until_active(gateway, 1)
    duplicate = asyncio.create_task(
        bridge.turn(identity=msg(7, 10), text="one", user_id=1)
    )
    second = asyncio.create_task(
        bridge.turn(identity=msg(7, 11), text="two", user_id=1)
    )
    await settle()
    assert gateway.calls == ["telegram:7:-:message:10:0"]

    gateway.release.set()
    assert (await first).payload["reply"] == "one"
    assert (await duplicate).payload["reply"] == "one"
    assert (await second).payload["reply"] == "two"
    assert gateway.calls == [
        "telegram:7:-:message:10:0",
        "telegram:7:-:message:11:0",
    ]
    assert gateway.max_active == 1
    assert (await duplicate).duplicate is True

    await bridge.close()
    assert bridge.session_count == 0


async def test_independent_chats_run_in_parallel() -> None:
    gateway = FakeGateway()
    bridge = TelegramBridge(gateway)
    tasks = [
        asyncio.create_task(bridge.turn(identity=msg(c, 1), text="x", user_id=1))
        for c in (100, 200, 300)
    ]
    # Wave 4: Windows event-loop scheduling is slower — poll for the three
    # concurrent turns instead of a fixed 10ms sleep.
    for _ in range(100):
        if gateway.max_active == 3:
            break
        await asyncio.sleep(0.01)
    assert gateway.max_active == 3, "independent chats must not serialise"
    gateway.release.set()
    await asyncio.gather(*tasks)
    await bridge.close()


async def test_concurrent_duplicates_invoke_the_runtime_once() -> None:
    gateway = FakeGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway)
    identity = msg(11, 55)
    results = await asyncio.gather(
        *(bridge.turn(identity=identity, text="same", user_id=1) for _ in range(8))
    )
    assert len(gateway.calls) == 1
    assert all(r.payload["reply"] == "same" for r in results)
    await bridge.close()


async def test_gateway_failure_is_retryable_without_poisoning_dedup() -> None:
    class FlakyGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def send_dialogue_turn(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("gateway unreachable")
            return {"reply": "recovered"}

    gateway = FlakyGateway()
    bridge = TelegramBridge(gateway)
    identity = msg(12, 77)

    with pytest.raises(ConnectionError):
        await bridge.turn(identity=identity, text="hi", user_id=1)
    assert await bridge.ledger.state_of(identity.key) is TurnState.FAILED

    retried = await bridge.turn(identity=identity, text="hi", user_id=1)
    assert retried.payload["reply"] == "recovered"
    assert gateway.calls == 2
    await bridge.close()


async def test_original_edit_and_duplicate_edit_have_distinct_identity() -> None:
    gateway = FakeGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway)

    original = msg(5, 100)
    edit = msg(5, 100, kind="edited", revision=1_700_000_000)

    await bridge.turn(identity=original, text="original", user_id=1)
    await bridge.turn(identity=edit, text="edited", user_id=1)
    replayed = await bridge.turn(identity=edit, text="edited", user_id=1)

    assert gateway.texts == ["original", "edited"]
    assert replayed.replayed is True
    await bridge.close()


async def test_same_message_id_in_two_chats_stays_isolated() -> None:
    gateway = FakeGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway)
    await bridge.turn(identity=msg(1, 42), text="chat one", user_id=1)
    await bridge.turn(identity=msg(2, 42), text="chat two", user_id=1)
    assert gateway.texts == ["chat one", "chat two"]
    await bridge.close()


async def test_forum_topics_are_isolated_sessions() -> None:
    seen: list[str] = []

    class RecordingGateway:
        async def send_dialogue_turn(self, **kwargs: Any) -> dict[str, Any]:
            seen.append(str(kwargs["session_id"]))
            return {"reply": "ok"}

    bridge = TelegramBridge(RecordingGateway())
    await bridge.turn(identity=msg(9, 1), text="a", user_id=1)
    await bridge.turn(identity=msg(9, 2, thread_id=77), text="b", user_id=1)
    assert seen == ["telegram:9", "telegram:9:topic:77"]
    await bridge.close()


# ── Durability across restart ────────────────────────────────────────────────


async def test_completed_turn_replays_after_restart(tmp_path: Path) -> None:
    ledger_path = tmp_path / "turns.db"
    identity = msg(21, 5)

    gateway = FakeGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway, ledger=TurnLedger(ledger_path))
    first = await bridge.turn(identity=identity, text="do it", user_id=1)
    assert first.payload["reply"] == "do it"
    await bridge.close()

    after = FakeGateway()
    after.release.set()
    restarted = TelegramBridge(after, ledger=TurnLedger(ledger_path))
    replay = await restarted.turn(identity=identity, text="do it", user_id=1)
    assert replay.replayed is True
    assert replay.payload["reply"] == "do it"
    assert after.calls == [], "a completed turn must never reach the runtime twice"
    await restarted.close()


async def test_ambiguous_inflight_turn_is_never_reinvoked(tmp_path: Path) -> None:
    """A turn a crashed process left in flight may already have run."""
    ledger_path = tmp_path / "turns.db"
    identity = msg(22, 6)

    crashed = TurnLedger(ledger_path)
    await crashed.claim(identity.key, identity.chat_id)  # never settled
    await crashed.close()

    gateway = FakeGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway, ledger=TurnLedger(ledger_path))
    with pytest.raises(AmbiguousTurn):
        await bridge.turn(identity=identity, text="maybe ran", user_id=1)
    assert gateway.calls == []
    await bridge.close()


# ── Bounds, races, cancellation ──────────────────────────────────────────────


async def test_bounded_queue_overflow_and_cleanup_after_failure() -> None:
    gateway = FakeGateway()
    bridge = TelegramBridge(gateway, max_queue_per_session=1)
    active = asyncio.create_task(
        bridge.turn(identity=msg(8, 1), text="a", user_id=1)
    )
    await until_active(gateway, 1)
    queued = asyncio.create_task(
        bridge.turn(identity=msg(8, 2), text="b", user_id=1)
    )
    await settle()
    with pytest.raises(BridgeOverflow):
        await bridge.turn(identity=msg(8, 3), text="c", user_id=1)

    gateway.release.set()
    await active
    await queued
    await bridge.close()


async def test_overflow_keeps_the_rejected_turn_retryable() -> None:
    gateway = FakeGateway()
    bridge = TelegramBridge(gateway, max_queue_per_session=1)
    rejected = msg(31, 3)
    active = asyncio.create_task(
        bridge.turn(identity=msg(31, 1), text="a", user_id=1)
    )
    await until_active(gateway, 1)
    queued = asyncio.create_task(
        bridge.turn(identity=msg(31, 2), text="b", user_id=1)
    )
    await settle()
    with pytest.raises(BridgeOverflow):
        await bridge.turn(identity=rejected, text="c", user_id=1)

    # Rejected before submission, so it must not be remembered as done.
    assert await bridge.ledger.state_of(rejected.key) is TurnState.FAILED
    gateway.release.set()
    await active
    await queued

    recovered = await bridge.turn(identity=rejected, text="c", user_id=1)
    assert recovered.payload["reply"] == "c"
    await bridge.close()


async def test_many_chat_flood_is_globally_bounded() -> None:
    gateway = FakeGateway()
    bridge = TelegramBridge(gateway, max_queue_per_session=1, max_sessions=16)
    tasks = [
        asyncio.create_task(bridge.turn(identity=msg(c, 1), text="x", user_id=1))
        for c in range(400)
    ]
    await asyncio.sleep(0.05)
    assert bridge.session_count <= 16

    gateway.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    rejected = [o for o in outcomes if isinstance(o, BridgeCapacityExceeded)]
    assert rejected, "a flood must be shed, not queued without bound"
    assert len(gateway.calls) + len(rejected) == 400
    await bridge.close()


async def test_enqueue_during_drain_exit_is_not_lost() -> None:
    """The empty-queue check and worker teardown must be one atomic step."""
    gateway = FakeGateway()
    bridge = TelegramBridge(gateway)
    first = asyncio.create_task(
        bridge.turn(identity=msg(1, 1), text="a", user_id=1)
    )
    while not gateway.calls:
        await asyncio.sleep(0)

    await bridge._lock.acquire()
    second = asyncio.create_task(
        bridge.turn(identity=msg(1, 2), text="b", user_id=1)
    )
    await asyncio.sleep(0)  # second is now queued ahead of the worker's exit
    gateway.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    bridge._lock.release()

    await asyncio.wait_for(first, timeout=1.0)
    await asyncio.wait_for(second, timeout=1.0)
    assert gateway.texts == ["a", "b"]
    await bridge.close()


async def test_cancel_chat_drops_only_that_chat_and_stays_retryable() -> None:
    gateway = FakeGateway()
    bridge = TelegramBridge(gateway)
    victim = msg(40, 2)
    active = asyncio.create_task(
        bridge.turn(identity=msg(40, 1), text="running", user_id=1)
    )
    await until_active(gateway, 1)
    queued = asyncio.create_task(bridge.turn(identity=victim, text="q", user_id=1))
    other = asyncio.create_task(
        bridge.turn(identity=msg(41, 1), text="other chat", user_id=1)
    )
    await settle()

    assert await bridge.cancel_chat(40) == 1
    with pytest.raises(TurnCancelled):
        await queued
    assert await bridge.ledger.state_of(victim.key) is TurnState.FAILED

    gateway.release.set()
    await active
    assert (await other).payload["reply"] == "other chat"
    await bridge.close()


async def test_cancel_chat_is_idempotent_and_safe_when_idle() -> None:
    gateway = FakeGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway)
    assert await bridge.cancel_chat(999) == 0
    assert await bridge.cancel_chat(999) == 0
    await bridge.close()


async def test_closed_bridge_refuses_new_turns() -> None:
    gateway = FakeGateway()
    gateway.release.set()
    bridge = TelegramBridge(gateway)
    await bridge.close()
    with pytest.raises(RuntimeError):
        await bridge.turn(identity=msg(1, 1), text="x", user_id=1)


# ── Inbound attachments ──────────────────────────────────────────────────────


def test_attachment_and_artifact_guards(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    target = prepare_inbound_path(incoming, "../../.env", size=12, max_size=20)
    assert target.parent == incoming.resolve()
    assert target.name == "env"
    with pytest.raises(AttachmentRejected):
        prepare_inbound_path(incoming, "x.bin", size=21, max_size=20)

    allowed = tmp_path / "workspace"
    allowed.mkdir()
    good = allowed / "report.txt"
    good.write_text("ok")
    assert resolve_outbound_artifact(good, (allowed,), max_size=10).path == good.resolve()
    secret = allowed / ".env"
    secret.write_text("TOKEN=x")
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(secret, (allowed,), max_size=100)
    link = allowed / "link.txt"
    link.symlink_to(good)
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(link, (allowed,), max_size=100)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/absolute/path.txt", "path.txt"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("réport✓.txt", "r_port_.txt"),
        ("bad\x00name\ttab.txt", "bad_name_tab.txt"),
        (".env.production", "env.production"),
        ("...", "attachment"),
        (None, "attachment"),
        ("CON", "file_CON"),
        ("nul.txt", "file_nul.txt"),
    ],
)
def test_sanitize_filename_neutralises_hostile_names(
    raw: str | None,
    expected: str,
) -> None:
    result = sanitize_filename(raw)
    assert "/" not in result and "\\" not in result
    assert result == expected


def test_sanitize_filename_is_bounded() -> None:
    assert len(sanitize_filename("a" * 5000 + ".txt")) <= 180


def test_inbound_slot_is_exclusive_nofollow_and_cleans_up(tmp_path: Path) -> None:
    root = tmp_path / "dl"
    slot = prepare_inbound_file(root, "report.txt", size=10, max_size=100)
    try:
        assert slot.temporary.exists()
        assert slot.path.parent == root.resolve()
        assert slot.path.name.endswith("_report.txt")
        # A second slot for the same logical name must not collide.
        other = prepare_inbound_file(root, "report.txt", size=10, max_size=100)
        assert other.temporary != slot.temporary
        discard_inbound_file(other)
    finally:
        discard_inbound_file(slot)
    assert not slot.temporary.exists()


def test_inbound_finalize_enforces_post_download_size(tmp_path: Path) -> None:
    import os

    slot = prepare_inbound_file(tmp_path / "dl", "a.bin", size=1, max_size=64)
    try:
        os.write(slot.handle, b"x" * 200)  # server lied about file_size
        with pytest.raises(AttachmentRejected):
            finalize_inbound_file(slot, max_size=64)
        assert not slot.path.exists(), "oversized payload must not be promoted"
    finally:
        discard_inbound_file(slot)


def test_inbound_finalize_promotes_valid_download(tmp_path: Path) -> None:
    import os

    slot = prepare_inbound_file(tmp_path / "dl", "a.bin", size=4, max_size=64)
    try:
        os.write(slot.handle, b"data")
        assert finalize_inbound_file(slot, max_size=64) == 4
        assert slot.path.read_bytes() == b"data"
        assert not slot.temporary.exists()
    finally:
        discard_inbound_file(slot)


def test_unknown_size_is_accepted_but_bounded_after_download(tmp_path: Path) -> None:
    import os

    # Telegram omits file_size for some uploads; 0 must not mean "unlimited".
    slot = prepare_inbound_file(tmp_path / "dl", "u.bin", size=0, max_size=16)
    try:
        os.write(slot.handle, b"y" * 32)
        with pytest.raises(AttachmentRejected):
            finalize_inbound_file(slot, max_size=16)
    finally:
        discard_inbound_file(slot)


def test_inbound_slot_refuses_a_presubstituted_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink squatting on the temp path must not be written through."""
    import os

    root = tmp_path / "dl"
    root.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("original")

    # Force the random suffix to a known value so the attacker can squat on it.
    monkeypatch.setattr(
        "antigona.channels.telegram.bridge.secrets.token_hex",
        lambda _n: "deadbeefdeadbeef",
    )
    squatted = root.resolve() / "deadbeefdeadbeef_x.bin.part"
    os.symlink(victim, squatted)

    with pytest.raises(AttachmentRejected):
        prepare_inbound_file(root, "x.bin", size=1, max_size=64)
    assert victim.read_text() == "original"


# ── Outbound artifacts ───────────────────────────────────────────────────────


def test_outbound_artifact_reads_through_one_validated_descriptor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    good = root / "report.txt"
    good.write_bytes(b"payload")
    artifact = resolve_outbound_artifact(good, (root,))
    assert artifact.data == b"payload"
    assert artifact.size == 7
    assert artifact.name == "report.txt"
    assert artifact.path == good.resolve()


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "server.key",
        "cert.pem",
        "vault.kdbx",
        "my_credentials.txt",
        "app-secret.json",
        "API_KEY.txt",
        "token",
        ".git-credentials",
        ".netrc",
    ],
)
def test_outbound_refuses_credential_families(tmp_path: Path, name: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / name
    target.write_text("sensitive")
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(target, (root,))


def test_outbound_refuses_secret_directories(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / ".ssh").mkdir(parents=True)
    target = root / ".ssh" / "notes.txt"
    target.write_text("x")
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(target, (root,))


def test_outbound_refuses_paths_outside_allowed_roots(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(outside, (root,))
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(root / ".." / "elsewhere.txt", (root,))


def test_outbound_refuses_symlinked_parent_component(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.txt").write_text("x")
    root.mkdir()
    (root / "alias").symlink_to(real)
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(root / "alias" / "file.txt", (root,))


@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo does not exist on Windows (Wave 4)")
def test_outbound_refuses_oversize_and_non_regular(tmp_path: Path) -> None:
    import os

    root = tmp_path / "workspace"
    root.mkdir()
    big = root / "big.bin"
    big.write_bytes(b"z" * 100)
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(big, (root,), max_size=10)

    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(fifo, (root,))


def test_outbound_requires_configured_roots(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("x")
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(target, ())


def test_outbound_refuses_relative_paths() -> None:
    with pytest.raises(AttachmentRejected):
        resolve_outbound_artifact(Path("relative.txt"), (Path("/tmp"),))


# ── Long messages ────────────────────────────────────────────────────────────


def test_long_message_split_preserves_order_and_limits() -> None:
    text = ("alpha beta gamma\n" * 800).strip()
    chunks = split_telegram_text(text, limit=512)
    assert "".join(chunks) == text
    assert all(utf16_length(chunk) <= 512 for chunk in chunks)


def test_split_counts_utf16_units_not_characters() -> None:
    # Every emoji is a surrogate pair: 2 UTF-16 units for 1 Python character.
    text = "🙂" * 100
    chunks = split_telegram_text(text, limit=10)
    assert "".join(chunks) == text
    assert all(utf16_length(chunk) <= 10 for chunk in chunks)
    assert len(chunks) == 20, "a character-based split would produce 10"


def test_split_never_bisects_an_html_entity() -> None:
    body = "&lt;tag&gt;" * 200
    chunks = split_telegram_html(body, limit=17)
    assert "".join(chunks) == body
    for chunk in chunks:
        assert utf16_length(chunk) <= 17
        assert chunk.count("&") == chunk.count(";"), chunk
        assert not chunk.endswith("&lt")


def test_split_handles_empty_and_rejects_bad_limits() -> None:
    assert split_telegram_text("") == [""]
    assert split_telegram_html("") == [""]
    with pytest.raises(ValueError):
        split_telegram_text("x", limit=0)
    with pytest.raises(ValueError):
        split_telegram_html("x", limit=0)
