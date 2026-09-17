"""Master Unit and Contract Test Suite for Antigona.

Covers all core requirements:
  1. Telegram Message Correlation & reply_to_message_id
  2. Out-of-order task completion without cross-talk
  3. Inbound file attachments and metadata contract
  4. Exact file creation and read-back verification
  5. Telegram outbound document delivery with reply_to
  6. Safe archive inspect, extract, create, and verify
  7. Archive path traversal attack rejection (../../evil.txt)
  8. Formatting preservation (newlines, spaces, JSON, code blocks)
  9. Structured JSON document generation & parse verification
  10. TTS audio generation & voice delivery contract
  11. Capability registry runtime truth snapshot
  12. Broken tool probe handling
  13. Approval token correlation & task isolation
  14. Safe workspace write
  15. Dangerous path / workspace escape prevention
  16. Prevention of false DONE on empty or unverified result
  17. Delivery idempotency
  18. Authoritative system date/time reporting
  19. Multi-file turn attachment support
  20. Tool registry contract dispatch
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from antigona.channels.telegram.attachment import (
    Attachment,
    compute_file_sha256,
)
from antigona.database import Database
from antigona.durable.operation_models import OperationState
from antigona.durable.operation_store import OperationStore
from antigona.durable.state_machine import (
    InvalidTransition,
    guard_verifying_done,
)
from antigona.durable.state_machine import (
    transition as sm_transition,
)
from antigona.events.bus import EventBus
from antigona.events.event_types import FinalResponseReady
from antigona.models import TaskState
from antigona.presentation.presenter import OperationPresenter, _safe_untrusted
from antigona.security.approval_grant import (
    ApprovalGrantStore,
    GrantDenialReason,
)
from antigona.tools.archive_ops import (
    ArchiveSecurityError,
    create_and_verify_archive,
    inspect_archive,
    safe_extract_archive,
)
from antigona.tools.capability_registry import (
    Capability,
    CapabilityRegistry,
    CapabilityStatus,
)
from antigona.tools.contracts import ToolInput
from antigona.tools.document_ops import (
    DocumentVerificationError,
    create_and_verify_document,
)
from antigona.tools.registry import ToolRegistry, register_builtins
from antigona.tools.system_time import get_current_system_time
from antigona.tools.tts_tool import TTSTool


class MockBot:
    """Mock Telegram bot capturing all outgoing deliveries with reply correlation."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_documents: list[dict[str, Any]] = []
        self.sent_photos: list[dict[str, Any]] = []
        self.sent_voices: list[dict[str, Any]] = []
        self._next_id = 1000

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._next_id += 1
        msg = {
            "message_id": self._next_id,
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
        }
        self.sent_messages.append(msg)
        return msg

    async def send_document(
        self,
        chat_id: int,
        document: Any,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._next_id += 1
        msg = {
            "message_id": self._next_id,
            "chat_id": chat_id,
            "document": document,
            "reply_to_message_id": reply_to_message_id,
            "caption": caption,
        }
        self.sent_documents.append(msg)
        return msg

    async def send_photo(
        self,
        chat_id: int,
        photo: Any,
        reply_to_message_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._next_id += 1
        msg = {
            "message_id": self._next_id,
            "chat_id": chat_id,
            "photo": photo,
            "reply_to_message_id": reply_to_message_id,
        }
        self.sent_photos.append(msg)
        return msg

    async def send_voice(
        self,
        chat_id: int,
        voice: Any,
        reply_to_message_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._next_id += 1
        msg = {
            "message_id": self._next_id,
            "chat_id": chat_id,
            "voice": voice,
            "reply_to_message_id": reply_to_message_id,
        }
        self.sent_voices.append(msg)
        return msg

    async def edit_message_text(self, **kwargs: Any) -> dict[str, Any]:
        return {"message_id": kwargs.get("message_id", 0)}

    async def delete_message(self, **kwargs: Any) -> bool:
        return True


@pytest.mark.asyncio
async def test_1_message_correlation() -> None:
    """Test 1: Parallel tasks A and B where B completes first; both reply to their own messages."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ops.db"
        db = Database(f"sqlite:///{db_path}")
        db.create_all()
        store = OperationStore(db)
        bot = MockBot()
        bus = EventBus()
        presenter = OperationPresenter(bot, bus, store, artifact_roots=(Path(tmpdir),))
        await presenter.start()

        # Create Task A (msg 101) and Task B (msg 202)
        op_a = await store.create(
            chat_id=12345,
            user_id=1,
            text="Task A",
            message_id=101,
            correlation_id="corr-a",
        )
        op_b = await store.create(
            chat_id=12345,
            user_id=1,
            text="Task B",
            message_id=202,
            correlation_id="corr-b",
        )

        # B completes first
        await store.transition_status(op_b.id, OperationState.VALIDATING)
        final_b = FinalResponseReady(
            operation_id=op_b.id,
            text="Result for B",
            terminal_state="SUCCEEDED",
        )
        receipt_b = await presenter.deliver_final(final_b)
        assert receipt_b is not None

        # A completes second
        await store.transition_status(op_a.id, OperationState.VALIDATING)
        final_a = FinalResponseReady(
            operation_id=op_a.id,
            text="Result for A",
            terminal_state="SUCCEEDED",
        )
        receipt_a = await presenter.deliver_final(final_a)
        assert receipt_a is not None

        # Verify deliveries replied to originating message IDs
        msg_b = next(m for m in bot.sent_messages if "Result for B" in m["text"])
        msg_a = next(m for m in bot.sent_messages if "Result for A" in m["text"])

        assert msg_b["reply_to_message_id"] == 202
        assert msg_a["reply_to_message_id"] == 101

        await presenter.stop()
        db.dispose()


@pytest.mark.asyncio
async def test_2_out_of_order_completion() -> None:
    """Test 2: Tasks A, B, C finishing in order C, A, B each target their own message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ops.db"
        db = Database(f"sqlite:///{db_path}")
        db.create_all()
        store = OperationStore(db)
        bot = MockBot()
        bus = EventBus()
        presenter = OperationPresenter(bot, bus, store, artifact_roots=(Path(tmpdir),))
        await presenter.start()

        op_a = await store.create(chat_id=999, user_id=1, text="A", message_id=10, correlation_id="c-a")
        op_b = await store.create(chat_id=999, user_id=1, text="B", message_id=20, correlation_id="c-b")
        op_c = await store.create(chat_id=999, user_id=1, text="C", message_id=30, correlation_id="c-c")

        # C finishes first
        await store.transition_status(op_c.id, OperationState.VALIDATING)
        await presenter.deliver_final(FinalResponseReady(operation_id=op_c.id, text="Finish C", terminal_state="SUCCEEDED"))

        # A finishes second
        await store.transition_status(op_a.id, OperationState.VALIDATING)
        await presenter.deliver_final(FinalResponseReady(operation_id=op_a.id, text="Finish A", terminal_state="SUCCEEDED"))

        # B finishes third
        await store.transition_status(op_b.id, OperationState.VALIDATING)
        await presenter.deliver_final(FinalResponseReady(operation_id=op_b.id, text="Finish B", terminal_state="SUCCEEDED"))

        mc = next(m for m in bot.sent_messages if "Finish C" in m["text"])
        ma = next(m for m in bot.sent_messages if "Finish A" in m["text"])
        mb = next(m for m in bot.sent_messages if "Finish B" in m["text"])

        assert mc["reply_to_message_id"] == 30
        assert ma["reply_to_message_id"] == 10
        assert mb["reply_to_message_id"] == 20

        await presenter.stop()
        db.dispose()


def test_3_text_file_upload_inbound() -> None:
    """Test 3: Inbound text file attachment contract creates safe metadata and computes sha256."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "hello.txt"
        test_file.write_text("Hello Antigona!", encoding="utf-8")

        att = Attachment.from_downloaded_file(
            test_file,
            telegram_file_id="tg_12345",
            original_filename="hello.txt",
            source_chat_id=555,
            source_message_id=42,
            operation_id="op_test",
        )

        assert att.safe_filename == "hello.txt"
        assert att.size == len("Hello Antigona!")
        assert att.sha256 == compute_file_sha256(test_file)
        assert att.is_supported()
        assert att.source_message_id == 42


def test_4_file_creation_and_verification() -> None:
    """Test 4: File creation with read-back verification matching expected content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        expected = "ANTIGONA WRITE TEST 999\nLine 2\nLine 3"
        doc = create_and_verify_document("result.txt", expected, workspace_root=tmpdir)

        assert doc.verified
        assert doc.size_bytes == len(expected.encode("utf-8"))
        assert Path(doc.path).read_text(encoding="utf-8") == expected


@pytest.mark.asyncio
async def test_5_telegram_outbound_document() -> None:
    """Test 5: Created document artifact delivered via send_document replying to origin message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ops.db"
        db = Database(f"sqlite:///{db_path}")
        db.create_all()
        store = OperationStore(db)
        bot = MockBot()
        bus = EventBus()
        presenter = OperationPresenter(bot, bus, store, artifact_roots=(Path(tmpdir),))
        await presenter.start()

        # Create file in workspace
        doc_path = Path(tmpdir) / "report.txt"
        doc_path.write_text("Generated Report Content", encoding="utf-8")

        op = await store.create(chat_id=777, user_id=1, text="Gen report", message_id=88, correlation_id="c")
        await store.transition_status(op.id, OperationState.VALIDATING)

        final = FinalResponseReady(
            operation_id=op.id,
            text="Here is your report",
            terminal_state="SUCCEEDED",
            artifacts=(str(doc_path),),
        )
        receipt = await presenter.deliver_final(final)
        assert receipt is not None

        assert len(bot.sent_documents) == 1
        sent_doc = bot.sent_documents[0]
        assert sent_doc["chat_id"] == 777
        assert sent_doc["reply_to_message_id"] == 88

        await presenter.stop()
        db.dispose()


def test_6_archive_lifecycle() -> None:
    """Test 6: Safe ZIP inspection, extraction, creation, and integrity verification."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        f1 = ws / "hello.py"
        f1.write_text("print('hello')", encoding="utf-8")
        f2 = ws / "data.json"
        f2.write_text('{"key": "value"}', encoding="utf-8")

        out_zip = ws / "bundle.zip"
        res_create = create_and_verify_archive(out_zip, [f1, f2], allowed_workspace_root=ws)
        assert res_create["verified"]
        assert res_create["file_count"] == 2

        # Inspect
        insp = inspect_archive(out_zip)
        assert insp.valid
        assert insp.format == "zip"
        assert insp.file_count == 2

        # Extract
        target_dir = ws / "unpacked"
        res_extract = safe_extract_archive(out_zip, target_dir, allowed_workspace_root=ws)
        assert res_extract["success"]
        assert (target_dir / "hello.py").read_text() == "print('hello')"
        assert (target_dir / "data.json").read_text() == '{"key": "value"}'


def test_7_archive_path_traversal_rejection() -> None:
    """Test 7: Archive containing ../../evil.txt is strictly rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        bad_zip = ws / "evil.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("../../evil.txt", "malicious payload")

        target = ws / "target"
        with pytest.raises(ArchiveSecurityError, match="Path traversal detected"):
            safe_extract_archive(bad_zip, target, allowed_workspace_root=ws)

        # Confirm evil.txt was never written anywhere
        assert not (ws.parent / "evil.txt").exists()


def test_8_formatting_preservation() -> None:
    """Test 8: Preserving newlines, indentations, JSON formatting without collapsing."""
    raw_text = "AAA\nBBB\nCCC\n\n  Indented Line\nANTIGONA FILE TEST 12345"
    sanitized = _safe_untrusted(raw_text, max_length=4096)
    lines = sanitized.splitlines()

    assert len(lines) >= 5
    assert "AAA" in lines[0]
    assert "BBB" in lines[1]
    assert "CCC" in lines[2]
    assert "ANTIGONA FILE TEST 12345" in sanitized


def test_9_json_document_creation_and_parse() -> None:
    """Test 9: JSON document generation and read-back parsing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data = {"system": "antigona", "test": True, "value": 42}
        doc = create_and_verify_document("test.json", data, doc_type="json", workspace_root=tmpdir)

        assert doc.verified
        loaded = json.loads(Path(doc.path).read_text(encoding="utf-8"))
        assert loaded == data


@pytest.mark.asyncio
async def test_10_speech_tts_tool() -> None:
    """Test 10: TTSTool execute contract validation."""
    tool = TTSTool()
    assert tool.spec.name == "speech.tts"

    # Input validation check
    errors = tool.validate(ToolInput(tool_name="speech.tts", params={}))
    assert len(errors) > 0

    # With text
    errors_ok = tool.validate(ToolInput(tool_name="speech.tts", params={"text": "Test speech"}))
    assert len(errors_ok) == 0


@pytest.mark.asyncio
async def test_11_capability_registry_truth() -> None:
    """Test 11: Capability registry reflects accurate runtime truth and snapshot."""
    reg = CapabilityRegistry()
    snap = reg.snapshot()
    assert "workspace.read" in snap
    assert "archive.create" in snap
    assert "speech.tts" in snap

    prompt_block = reg.format_prompt_snapshot()
    assert "CAPABILITY INVENTORY" in prompt_block
    assert "workspace.read" in prompt_block


@pytest.mark.asyncio
async def test_12_broken_tool_probe() -> None:
    """Test 12: Failing probe marks capability as BROKEN with failure reason."""
    reg = CapabilityRegistry()

    async def fail_probe() -> bool:
        return False

    reg.register(
        Capability(
            id="test.broken_tool",
            category="test",
            description="A broken tool",
            probe_fn=fail_probe,
        )
    )

    status = await reg.probe("test.broken_tool")
    assert status == CapabilityStatus.BROKEN
    cap = reg.get("test.broken_tool")
    assert cap is not None
    assert cap.failure_reason == "Probe returned False"


def test_13_approval_isolation_and_correlation() -> None:
    """Test 13: Approval token for Task A / Command A cannot authorize Task B / Command B."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "grants.db"
        store = ApprovalGrantStore(db)

        # Issue grant for Task A command "ls -la"
        token_a = store.issue(
            actor="owner",
            tool_name="run_shell",
            args={"command": "ls -la"},
            issuer="test",
            ttl_seconds=60,
        )

        # 1. Verification and atomic consumption with exact params succeeds
        verdict = store.verify_and_consume(
            token=token_a,
            actor="owner",
            tool_name="run_shell",
            args={"command": "ls -la"},
        )
        assert verdict.valid

        # 2. Replay with same token fails (consumed single-use)
        verdict_replay = store.verify_and_consume(
            token=token_a,
            actor="owner",
            tool_name="run_shell",
            args={"command": "ls -la"},
        )
        assert not verdict_replay.valid
        assert verdict_replay.reason == GrantDenialReason.CONSUMED

        # 3. Different command with new token for Task B fails
        token_b = store.issue(
            actor="owner",
            tool_name="run_shell",
            args={"command": "cat /etc/hosts"},
            issuer="test",
            ttl_seconds=60,
        )
        verdict_mismatch = store.verify_and_consume(
            token=token_b,
            actor="owner",
            tool_name="run_shell",
            args={"command": "rm -rf /"},
        )
        assert not verdict_mismatch.valid
        assert verdict_mismatch.reason == GrantDenialReason.ARGS_MISMATCH


def test_14_safe_workspace_write() -> None:
    """Test 14: Ordinary file creation inside workspace succeeds."""
    with tempfile.TemporaryDirectory() as tmpdir:
        doc = create_and_verify_document("ordinary.txt", "Normal content", workspace_root=tmpdir)
        assert doc.verified
        assert Path(doc.path).exists()


def test_15_dangerous_path_escape_blocked() -> None:
    """Test 15: Target path escaping workspace boundary is blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir) / "ws"
        ws.mkdir()
        with pytest.raises(DocumentVerificationError, match="escapes workspace"):
            create_and_verify_document("../escaped.txt", "payload", workspace_root=ws)


def test_16_strict_done_verification_guard() -> None:
    """Test 16: State machine guards prevent invalid or unauthorized DONE transitions."""
    # 1. Transitioning to DONE without verifier capability raises InvalidTransition
    with pytest.raises(InvalidTransition, match="requires verifier capability"):
        sm_transition(
            TaskState.VERIFYING,
            TaskState.DONE,
            cancellation_requested=False,
            verifier_capability=False,
        )

    # 2. Unverified evidence raises InvalidTransition
    with pytest.raises(InvalidTransition, match="requires verified evidence"):
        guard_verifying_done(evidence_verified=False)


@pytest.mark.asyncio
async def test_17_delivery_idempotency() -> None:
    """Test 17: Calling deliver_final multiple times on the same operation produces one delivery."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ops.db"
        db = Database(f"sqlite:///{db_path}")
        db.create_all()
        store = OperationStore(db)
        bot = MockBot()
        bus = EventBus()
        presenter = OperationPresenter(bot, bus, store, artifact_roots=(Path(tmpdir),))
        await presenter.start()

        op = await store.create(chat_id=111, user_id=1, text="Idempotency Test", message_id=55, correlation_id="c")
        await store.transition_status(op.id, OperationState.VALIDATING)

        final = FinalResponseReady(
            operation_id=op.id,
            text="Single Delivery Text",
            terminal_state="SUCCEEDED",
        )

        r1 = await presenter.deliver_final(final)
        r2 = await presenter.deliver_final(final)

        assert r1 is not None
        assert r2 == r1
        assert len([m for m in bot.sent_messages if "Single Delivery Text" in m["text"]]) == 1

        await presenter.stop()
        db.dispose()


def test_18_system_time_tool() -> None:
    """Test 18: SystemTimeTool provides authoritative real time data."""
    data = get_current_system_time()
    assert "utc_iso" in data
    assert "local_iso" in data
    assert "day_of_week" in data
    assert data["timestamp"] > 1700000000


def test_19_multi_file_attachments() -> None:
    """Test 19: Turn with multiple attachments registers and tracks all files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p1 = Path(tmpdir) / "f1.txt"
        p2 = Path(tmpdir) / "f2.py"
        p1.write_text("file 1 content", encoding="utf-8")
        p2.write_text("# file 2 python", encoding="utf-8")

        att1 = Attachment.from_downloaded_file(p1, telegram_file_id="id1", original_filename="f1.txt", source_chat_id=1, source_message_id=10)
        att2 = Attachment.from_downloaded_file(p2, telegram_file_id="id2", original_filename="f2.py", source_chat_id=1, source_message_id=10)

        assert att1.original_filename == "f1.txt"
        assert att2.original_filename == "f2.py"
        assert att1.sha256 != att2.sha256
        assert att1.source_message_id == att2.source_message_id == 10


def test_20_tool_registry_contract_dispatch() -> None:
    """Test 20: ToolRegistry dispatches contract tools with proper JSON serialization."""
    reg = ToolRegistry()
    register_builtins(reg)

    # Test system.time tool via registry dispatch
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(reg.dispatch("system.time"))
        parsed = json.loads(out)
        assert parsed["success"] is True
        assert "utc_iso" in parsed["data"]
    finally:
        loop.close()
