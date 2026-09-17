from __future__ import annotations

import sys
from pathlib import Path

import pytest

from antigona.security import read_private_credential, verifier_credential


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX st_mode bits (0o600/0o640/0o750) not enforceable on Windows (Wave 4) / POSIX uid semantics (geteuid/getuid/st_uid) unavailable on Windows (Wave 4)')
def test_verifier_credential_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "verifier.credential"
    path.write_text("file-secret\n")
    path.chmod(0o600)
    monkeypatch.delenv("ANTIGONA_VERIFIER_CREDENTIAL", raising=False)
    monkeypatch.setenv("ANTIGONA_VERIFIER_CREDENTIAL_FILE", str(path))
    assert verifier_credential() == "file-secret"


def test_verifier_credential_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTIGONA_VERIFIER_CREDENTIAL_FILE", raising=False)
    monkeypatch.setenv("ANTIGONA_VERIFIER_CREDENTIAL", "env-secret")
    assert verifier_credential() == "env-secret"


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX st_mode bits (0o600/0o640/0o750) not enforceable on Windows (Wave 4) / POSIX uid semantics (geteuid/getuid/st_uid) unavailable on Windows (Wave 4)')
def test_read_private_credential_empty_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_text("   \n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="empty"):
        read_private_credential(str(path))


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX st_mode bits (0o600/0o640/0o750) not enforceable on Windows (Wave 4) / POSIX uid semantics (geteuid/getuid/st_uid) unavailable on Windows (Wave 4)')
def test_read_private_credential_wrong_owner_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "test.cred"
    path.write_text("secret-data\n")
    path.chmod(0o600)
    # Mock UID mismatch: file belongs to a DIFFERENT user than the running process
    monkeypatch.setattr("os.geteuid", lambda: 99999)
    from antigona.security._credentials import read_private_credential
    with pytest.raises(PermissionError):
        read_private_credential(str(path))
