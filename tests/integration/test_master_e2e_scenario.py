"""Master Live E2E Scenario Integration Test.

Simulates the complete user prompt workflow:
  «Вот архив проекта. Проверь проект, исправь ошибку, составь отчёт,
   упакуй результат, пришли обратно архив и пришли короткую голосовую сводку».

Pipeline:
  1. Inbound Telegram turn: message_id=5000, chat_id=1001, attachment=project.zip.
  2. Safe extraction of project.zip to workspace.
  3. Identification and fixing of bug in workspace source files.
  4. Exact verification of fix.
  5. Creation & read-back verification of report.md.
  6. Creation & checksum verification of result.zip.
  7. Generation of voice summary audio.
  8. Final response delivery: text + result.zip + voice summary audio.
  9. Verification that all deliveries replied strictly to message_id=5000.
  10. Verification that state machine reached SUCCEEDED / DONE only after proofs.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

import pytest

from antigona.channels.telegram.attachment import Attachment
from antigona.database import Database
from antigona.durable.operation_models import OperationState
from antigona.durable.operation_store import OperationStore
from antigona.events.bus import EventBus
from antigona.events.event_types import FinalResponseReady
from antigona.presentation.presenter import OperationPresenter
from antigona.tools.archive_ops import (
    create_and_verify_archive,
    safe_extract_archive,
)
from antigona.tools.document_ops import create_and_verify_document
from tests.unit.test_master_contract_suite import MockBot


@pytest.mark.asyncio
async def test_master_live_e2e_scenario() -> None:
    """Run full simulated Master E2E scenario."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_root = Path(tmpdir) / "workspace"
        ws_root.mkdir(parents=True, exist_ok=True)
        db_path = Path(tmpdir) / "e2e_ops.db"

        # ── Step 1: Prepare Inbound Project Archive with Bug ───────────────────
        input_project_dir = Path(tmpdir) / "inbound_project"
        input_project_dir.mkdir()
        buggy_code = input_project_dir / "calculator.py"
        buggy_code.write_text("def add(a, b):\n    return a - b  # BUG: subtraction instead of addition\n")
        readme = input_project_dir / "README.md"
        readme.write_text("# Math Project\nBuggy add function.\n")

        inbound_zip = Path(tmpdir) / "inbound_project.zip"
        with zipfile.ZipFile(inbound_zip, "w") as zf:
            zf.write(buggy_code, arcname="calculator.py")
            zf.write(readme, arcname="README.md")

        # ── Step 2: Receive Telegram Turn & Create Operation ──────────────────
        user_chat_id = 998877
        user_msg_id = 5000

        att = Attachment.from_downloaded_file(
            inbound_zip,
            telegram_file_id="tg_proj_zip_999",
            original_filename="inbound_project.zip",
            source_chat_id=user_chat_id,
            source_message_id=user_msg_id,
            caption="Вот архив проекта. Проверь проект, исправь ошибку, составь отчёт, упакуй результат, пришли обратно архив и пришли короткую голосовую сводку.",
        )
        assert att.sha256 != ""

        db = Database(f"sqlite:///{db_path}")
        db.create_all()
        store = OperationStore(db)
        bot = MockBot()
        bus = EventBus()
        presenter = OperationPresenter(bot, bus, store, artifact_roots=(ws_root,))
        await presenter.start()

        op = await store.create(
            chat_id=user_chat_id,
            user_id=1,
            text=att.caption,
            message_id=user_msg_id,
            correlation_id="corr_e2e_master",
        )
        assert op.origin_message_id == user_msg_id

        # ── Step 3: Safe Extraction to Workspace ──────────────────────────────
        extract_dir = ws_root / "extracted_project"
        extract_res = safe_extract_archive(
            inbound_zip,
            extract_dir,
            allowed_workspace_root=ws_root,
        )
        assert extract_res["success"]
        assert extract_res["file_count"] == 2
        assert (extract_dir / "calculator.py").exists()

        # ── Step 4: Fix Bug in Workspace Source Files ──────────────────────────
        target_calc = extract_dir / "calculator.py"
        fixed_code = "def add(a, b):\n    return a + b  # FIXED: addition corrected\n"
        target_calc.write_text(fixed_code, encoding="utf-8")

        # Verify fixed code
        assert target_calc.read_text(encoding="utf-8") == fixed_code

        # ── Step 5: Create and Verify Report Document ──────────────────────────
        report_text = (
            "# Project Audit & Bugfix Report\n\n"
            "## Summary\n"
            "- Bug in `calculator.py`: subtraction replaced with addition.\n"
            "- Tests: passed.\n"
            "- Archive: verified and repacked.\n"
        )
        report_doc = create_and_verify_document(
            "report.md",
            report_text,
            doc_type="markdown",
            workspace_root=ws_root,
        )
        assert report_doc.verified

        # ── Step 6: Create and Verify Output Archive ───────────────────────────
        out_zip = ws_root / "result.zip"
        res_archive = create_and_verify_archive(
            out_zip,
            extract_dir,
            format="zip",
            allowed_workspace_root=ws_root,
        )
        assert res_archive["verified"]
        assert res_archive["file_count"] == 2

        # ── Step 7: Create Voice Summary Audio ─────────────────────────────────
        voice_audio = ws_root / "summary.ogg"
        voice_audio.write_bytes(b"OggS\x00\x02\x00\x00\x00\x00\x00\x00mock_voice_audio_bytes")
        assert voice_audio.exists() and voice_audio.stat().st_size > 0

        # ── Step 8: Final Delivery via OperationPresenter ─────────────────────
        await store.transition_status(op.id, OperationState.VALIDATING)

        final_event = FinalResponseReady(
            operation_id=op.id,
            text=(
                "✅ Проект проверен и исправлен.\n"
                "Ошибка в сложении исправлена, отчёт и архив сформированы."
            ),
            terminal_state="SUCCEEDED",
            artifacts=(str(report_doc.path), str(out_zip), str(voice_audio)),
        )

        receipt = await presenter.deliver_final(final_event)
        assert receipt is not None

        # ── Step 9: Verify All Deliveries Target Original message_id ──────────
        # Check text message
        final_msg = next(m for m in bot.sent_messages if "Проект проверен" in m["text"])
        assert final_msg["chat_id"] == user_chat_id
        assert final_msg["reply_to_message_id"] == user_msg_id

        # Check document delivery
        assert len(bot.sent_documents) >= 1
        for sent_doc in bot.sent_documents:
            assert sent_doc["chat_id"] == user_chat_id
            assert sent_doc["reply_to_message_id"] == user_msg_id

        # Check voice delivery
        assert len(bot.sent_voices) == 1
        for sent_voice in bot.sent_voices:
            assert sent_voice["chat_id"] == user_chat_id
            assert sent_voice["reply_to_message_id"] == user_msg_id

        # ── Step 10: Verify Terminal State ────────────────────────────────────
        op_final = await store.get(op.id)
        assert op_final is not None
        assert op_final.status == OperationState.SUCCEEDED

        await presenter.stop()
