"""Unit tests for content-addressed skill store (P2.1.f)."""

from __future__ import annotations

import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from antigona.database import Database
from antigona.skills.canonical import is_sha256_hex
from antigona.skills.errors import SkillIntegrityError
from antigona.skills.format import render_card
from antigona.skills.lifecycle import SkillState
from antigona.skills.records import Origin, PlanStep, RiskCeiling, SkillCard, Trust, Verdict
from antigona.skills.registry import SkillsRegistry
from antigona.skills.store import CardStore, read_body_safely


def make_valid_card() -> bytes:
    card = SkillCard(
        skill_id="skl-9f3c2a5e-71b4-4f0d-9a6e-8c2d13f45b70",
        slug="workspace-report-scaffold",
        version=1,
        owner_id="owner-42",
        trust=Trust.TRUSTED,
        risk=RiskCeiling.LOW,
        intent=("Test card",),
        plan=(PlanStep(number=1, tool="workspace.mkdir", args=(("path", "reports"),)),),
        origin=Origin(
            flow="flow-123",
            steps=1,
            captured=datetime(2026, 7, 26, 10, 0, 0, tzinfo=UTC),
            verdict=Verdict.VERIFIER_PASS,
            trust_at_capture=Trust.TRUSTED,
        ),
    )
    return render_card(card)


@pytest.fixture
def db_session(tmp_path: Path) -> Session:
    db = Database(f"sqlite:///{tmp_path}/test.db")
    db.create_all()
    with db.session_factory() as session:
        yield session


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX st_mode (0o640/0o750) not enforceable on Windows (Wave 4)')
def test_body_file_mode_is_0640(tmp_path: Path) -> None:
    store = CardStore(str(tmp_path))
    canonical_body = make_valid_card()
    digest = store.write(canonical_body)

    file_path = store.path(digest)
    st = file_path.stat()
    mode = stat.S_IMODE(st.st_mode)
    assert mode == 0o640
    assert file_path.parent.stat().st_mode & 0o777 == 0o750


@pytest.mark.skipif(sys.platform == "win32", reason='symlink/hardlink/inode semantics unsupported on Windows (Wave 4)')
def test_refuses_symlink_body(tmp_path: Path) -> None:
    store = CardStore(str(tmp_path))
    canonical_body = make_valid_card()
    digest = store.write(canonical_body)
    file_path = store.path(digest)

    target_file = tmp_path / "secret.txt"
    target_file.write_text("secret")
    file_path.unlink()
    file_path.symlink_to(target_file)

    with pytest.raises(SkillIntegrityError, match="symlink"):
        read_body_safely(tmp_path, digest)


@pytest.mark.skipif(sys.platform == "win32", reason='symlink/hardlink/inode semantics unsupported on Windows (Wave 4)')
def test_refuses_hardlinked_body(tmp_path: Path) -> None:
    store = CardStore(str(tmp_path))
    canonical_body = make_valid_card()
    digest = store.write(canonical_body)
    file_path = store.path(digest)

    hardlink_path = tmp_path / "hardlink.txt"
    os.link(file_path, hardlink_path)

    with pytest.raises(SkillIntegrityError, match="hardlink"):
        read_body_safely(tmp_path, digest)


def test_refuses_path_escape(tmp_path: Path) -> None:
    with pytest.raises(SkillIntegrityError):
        read_body_safely(tmp_path, "../escape")


def test_is_sha256_hex_is_single_home() -> None:
    """Digest shape lives in canonical; store rejects non-digests via the same helper."""
    assert is_sha256_hex("a" * 64)
    assert not is_sha256_hex("A" * 64)  # uppercase rejected
    assert not is_sha256_hex("a" * 63)
    assert not is_sha256_hex("../escape")


def test_write_rechecks_digest_not_only_size(tmp_path: Path) -> None:
    """A same-size but different body on disk must be rewritten, not silently accepted."""
    store = CardStore(str(tmp_path))
    canonical_body = make_valid_card()
    digest = store.write(canonical_body)
    file_path = store.path(digest)

    tampered = canonical_body.replace(b"reports", b"tamperd")
    assert len(tampered) == len(canonical_body)
    assert tampered != canonical_body
    file_path.chmod(0o640)
    file_path.write_bytes(tampered)

    # Same size — the old size-only check would have returned early here.
    assert store.write(canonical_body) == digest
    assert store.read(digest) == canonical_body
    assert file_path.read_bytes() == canonical_body


def test_hash_mismatch_quarantines_skill(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    canonical_body = make_valid_card()

    skill = registry.register_skill(
        canonical_body, owner_id="owner-42", state_root=str(tmp_path)
    )

    store = CardStore(str(tmp_path))
    file_path = store.path(skill.body_sha256)

    tampered = canonical_body.replace(b"reports", b"tamperd")
    file_path.chmod(0o640)
    file_path.write_bytes(tampered)

    with pytest.raises(SkillIntegrityError):
        registry.get_card_body(skill, str(tmp_path))

    quarantined = registry.quarantine(skill, actor="verifier", reason="hash mismatch")
    assert quarantined.status == SkillState.QUARANTINED.value
