"""Unit tests for Antigona Runtime Provenance Guard."""

import sys

import pytest

from antigona.core import paths
from antigona.core.provenance_guard import (
    RuntimeProvenanceError,
    check_constitution,
    check_continuity_record,
    check_docker_proxy_contract,
    check_forbidden_paths,
    check_working_directory,
    default_canonical_root,
    get_current_git_sha,
    verify_provenance_or_fail,
)


def test_provenance_guard_fails_on_forbidden_sys_path(monkeypatch):
    """Verify fail-closed when forbidden legacy source path is injected into sys.path."""
    forbidden = f"{paths.home_dir()}/.antigona/src"
    monkeypatch.setattr(sys, "path", sys.path + [forbidden])
    result = check_forbidden_paths()
    assert not result.ok
    assert f"sys.path entry: {forbidden}" in result.detail


def test_provenance_guard_fails_on_forbidden_health_path(monkeypatch):
    """Verify fail-closed when forbidden legacy health path is injected into sys.path."""
    forbidden = f"{paths.home_dir()}/.antigona/.health"
    monkeypatch.setattr(sys, "path", sys.path + [forbidden])
    result = check_forbidden_paths()
    assert not result.ok
    assert f"sys.path entry: {forbidden}" in result.detail


def test_provenance_guard_fails_on_missing_continuity_record(tmp_path):
    """Verify fail-closed when STATE_ANTIGONA_CANON.md is missing."""
    result = check_continuity_record(tmp_path)
    assert not result.ok
    assert "missing" in result.detail


def test_provenance_guard_fails_on_corrupt_continuity_record(tmp_path):
    """Verify fail-closed when STATE_ANTIGONA_CANON.md has wrong schema."""
    fake_record = tmp_path / "STATE_ANTIGONA_CANON.md"
    fake_record.write_text("FAKE SCHEMA CONTENT")
    result = check_continuity_record(tmp_path)
    assert not result.ok
    assert "invalid record schema header" in result.detail


def test_provenance_guard_fails_on_constitution_tampering(tmp_path):
    """Verify fail-closed when constitution hash does not match."""
    aptechka = tmp_path / "aptechka"
    aptechka.mkdir()
    fake_const = aptechka / "CONSTITUTION.md"
    fake_const.write_text("TAMPERED CONSTITUTION")
    result = check_constitution(tmp_path)
    assert not result.ok
    assert "hash mismatch" in result.detail


def test_provenance_guard_fails_on_unauthorized_cwd(monkeypatch, tmp_path):
    """Verify fail-closed when cwd is in an unauthorized location."""
    isolated_dir = tmp_path / "isolated_unauthorized_dir"
    isolated_dir.mkdir()
    monkeypatch.chdir(isolated_dir)
    root = default_canonical_root()
    result = check_working_directory(root)
    assert not result.ok
    assert "outside canonical root" in result.detail


def test_docker_proxy_contract_passes(tmp_path):
    """Verify docker proxy contract succeeds on canonical proxy script."""
    root = default_canonical_root()
    result = check_docker_proxy_contract(root)
    assert result.ok
    assert "standalone deployment script" in result.detail


def test_docker_proxy_contract_fails_on_import_antigona(tmp_path):
    """Verify fail-closed if docker proxy imports antigona."""
    deploy_sandbox = tmp_path / "deploy" / "sandbox"
    deploy_sandbox.mkdir(parents=True)
    bad_proxy = deploy_sandbox / "docker_socket_proxy.py"
    bad_proxy.write_text("import antigona\nprint('bad')\n")
    result = check_docker_proxy_contract(tmp_path)
    assert not result.ok
    assert "forbidden import antigona" in result.detail


def test_verify_provenance_or_fail_raises(monkeypatch):
    """Verify RuntimeProvenanceError is raised when a check fails."""
    forbidden = f"{paths.home_dir()}/.antigona/src"
    monkeypatch.setattr(sys, "path", sys.path + [forbidden])
    root = default_canonical_root()
    current_sha = get_current_git_sha(root)
    with pytest.raises(RuntimeProvenanceError) as exc_info:
        verify_provenance_or_fail(root, current_sha)
    assert "Antigona Runtime Provenance Guard FAIL-CLOSED" in str(exc_info.value)
    # The raise must be caused by the injected forbidden path, not an unrelated
    # provenance check: pin the failing reason so the test cannot pass vacuously.
    assert f"sys.path entry: {forbidden}" in str(exc_info.value)
