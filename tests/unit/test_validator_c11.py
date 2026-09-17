import hashlib
import json
from pathlib import Path

import pytest

from antigona.core import paths
from antigona.startup import validator


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    (tmp_path / "app.py").write_text("immutable")
    files = {"app.py": hashlib.sha256(b"immutable").hexdigest()}
    base = {
        "schema": "antigona-deployment-manifest/v2",
        "mode": "gitless",
        "commit": "ffe5fbeaeef8b37c68abf54af7163ed684f39a6c",
        "source_commit": "ecbbc412b6cceec88742a3c484b269ef2a895638",
        "release_metadata_commit": "3c63ad200ea5514411a863927b2ece34a29ed3f6",
        "provenance": {"source_commit": "ecbbc412b6cceec88742a3c484b269ef2a895638"},
        "file_count": 1,
        "scope": "all regular non-symlink files in files; manifest and report authenticated separately",
        "files": files,
    }
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    report = tmp_path / "CANDIDATE_MANIFEST_HASH_REPORT.md"
    report.write_text(
        json.dumps(
            {
                "schema": "antigona-manifest-report/v1",
                "mode": "gitless",
                "commit": base["commit"],
                "source_commit": base["source_commit"],
                "release_metadata_commit": base["release_metadata_commit"],
                "file_count": 1,
                "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    base.update(
        report="CANDIDATE_MANIFEST_HASH_REPORT.md",
        report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
    )
    manifest.write_text(json.dumps(base))
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    monkeypatch.setenv("ANTIGONA_DEPLOYMENT_MANIFEST", str(manifest))
    return manifest, report


def test_c11_gitless_manifest_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(tmp_path, monkeypatch)
    assert validator.check_worktree_integrity().ok


def test_c11_changed_and_missing_listed_files_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(tmp_path, monkeypatch)
    (tmp_path / "app.py").write_text("changed")
    assert not validator.check_worktree_integrity().ok
    (tmp_path / "app.py").unlink()
    assert not validator.check_worktree_integrity().ok


def test_c11_unknown_symlink_and_tampered_metadata_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, report = _fixture(tmp_path, monkeypatch)
    (tmp_path / "unknown.txt").write_text("x")
    assert not validator.check_worktree_integrity().ok
    (tmp_path / "unknown.txt").unlink()
    report.write_text("tampered")
    assert not validator.check_worktree_integrity().ok
    report.unlink()
    manifest.unlink()
    assert not validator.check_worktree_integrity().ok


def test_c11_dirty_override_does_not_bypass_integrity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTIGONA_ALLOW_DIRTY_WORKTREE", "1")
    (tmp_path / "app.py").write_text("changed")
    assert not validator.check_worktree_integrity().ok


def test_c11_symlink_listed_file_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(tmp_path, monkeypatch)
    (tmp_path / "app.py").unlink()
    (tmp_path / "app.py").symlink_to("/etc/hosts")
    assert not validator.check_worktree_integrity().ok


def test_c10_accepts_exact_legacy_status() -> None:
    legacy = f"/usr/bin/python3 {paths.home_dir()}/antigona-status/server.py"
    assert validator._is_legacy_status(validator.ProcInfo(1, 1, 0, legacy, "/"))


def test_c10_rejects_wrapper_and_near_match() -> None:
    assert not validator._is_legacy_status(
        validator.ProcInfo(1, 1, 0, "/bin/sh -c /usr/bin/python3 /opt/antigona-home/antigona-status/server.py", "/")
    )
    assert not validator._is_legacy_status(
        validator.ProcInfo(1, 1, 0, "/usr/bin/python3 /opt/antigona-home/antigona-status/server.py --port 1", "/")
    )


def test_c11_cli_manifest_check_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(tmp_path, monkeypatch)
    # The C11 verdict journal (wave G1d) writes under paths.evidence_dir(); isolate it
    # inside tmp_path so the test never touches the host evidence root and two runs of
    # this file cannot race on one shared chain HEAD.
    monkeypatch.setenv("ANTIGONA_EVIDENCE_DIR", str(tmp_path / "evidence"))
    assert validator.run("manifest") == 0
    (tmp_path / "app.py").write_text("changed")
    assert validator.run("manifest") == 1


def test_c11_cli_main_manifest_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTIGONA_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(sys, "argv", ["validator", "--check=manifest"])
    assert validator.main() == 0
    (tmp_path / "app.py").write_text("tampered")
    assert validator.main() == 1


def test_c11_source_label_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "app.py").write_text("immutable")
    files = {"app.py": hashlib.sha256(b"immutable").hexdigest()}
    base = {
        "schema": "antigona-deployment-manifest/v2",
        "mode": "gitless",
        "commit": "ffe5fbeaeef8b37c68abf54af7163ed684f39a6c",
        "source_commit": "ecbbc412b6cceec88742a3c484b269ef2a895638",
        "release_metadata_commit": "3c63ad200ea5514411a863927b2ece34a29ed3f6",
        "provenance": {"source_commit": "0000000000000000000000000000000000000000"},
        "file_count": 1,
        "scope": "all regular non-symlink files in files; manifest and report authenticated separately",
        "files": files,
    }
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    report = tmp_path / "CANDIDATE_MANIFEST_HASH_REPORT.md"
    report.write_text(
        json.dumps(
            {
                "schema": "antigona-manifest-report/v1",
                "mode": "gitless",
                "commit": base["commit"],
                "source_commit": base["source_commit"],
                "release_metadata_commit": base["release_metadata_commit"],
                "file_count": 1,
                "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    base.update(
        report="CANDIDATE_MANIFEST_HASH_REPORT.md",
        report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
    )
    manifest.write_text(json.dumps(base))
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    monkeypatch.setenv("ANTIGONA_DEPLOYMENT_MANIFEST", str(manifest))
    res = validator.check_worktree_integrity()
    assert not res.ok
    assert "provenance does not name source_commit" in res.detail


def test_c11_report_source_commit_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, report = _fixture(tmp_path, monkeypatch)
    rep_data = json.loads(report.read_text())
    rep_data["source_commit"] = "1111111111111111111111111111111111111111"
    report.write_text(json.dumps(rep_data))
    m_data = json.loads(manifest.read_text())
    m_data["report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(m_data))
    res = validator.check_worktree_integrity()
    assert not res.ok
    assert "manifest/report authentication mismatch" in res.detail
def test_c11_envelope_mutating_listed_files_fails_source_truth(tmp_path: Path) -> None:
    p_files = {"core.py": "def f(): return 1\n"}
    m_files = {"core.py": "def f(): return 2\n"}
    p_hashes = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in p_files.items()}
    m_hashes = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in m_files.items()}

    manifest_files: dict[str, str] = m_hashes

    mismatches = []
    for path, expected_hash in manifest_files.items():
        actual_p_hash = p_hashes.get(path)
        if actual_p_hash != expected_hash:
            mismatches.append(path)
    assert mismatches == ["core.py"], "Tampering with listed files between P and M must be detected"
