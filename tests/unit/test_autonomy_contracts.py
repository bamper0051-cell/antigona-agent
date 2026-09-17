from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from antigona.core import paths
from antigona.orchestration.autonomy import (
    AutonomyContractError,
    BoundaryResult,
    CandidateStore,
    StageContext,
    WorkspaceBoundary,
    evaluate_stage,
    hash_file,
    snapshot_workspace,
)
from antigona.orchestration.executors import ServiceExecutors


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "app.py").write_text("VALUE = 1\n")
    (workspace / "test_app.py").write_text("def test_value():\n    assert True\n")
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return workspace


def _analysis(workspace: Path, snapshot_hash: str) -> str:
    return json.dumps(
        {
            "kind": "analysis",
            "snapshot_hash": snapshot_hash,
            "summary": "The value is defined in app.py.",
            "evidence": [
                {"path": "app.py", "sha256": hash_file(workspace / "app.py"), "line": 1}
            ],
        }
    )


def test_refusal_or_arbitrary_text_cannot_satisfy_analysis(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    context = StageContext.analysis(workspace, before.tree_hash)
    with pytest.raises(AutonomyContractError):
        evaluate_stage(context, "I cannot inspect files, but the task is complete.")


def test_alternate_refusal_and_confident_hallucination_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    context = StageContext.analysis(workspace, before.tree_hash)
    for prose in (
        "Tool access is unavailable; nevertheless everything looks correct.",
        json.dumps({"kind": "analysis", "snapshot_hash": before.tree_hash,
                    "summary": "app.py is correct", "evidence": []}),
        json.dumps({"kind": "analysis", "snapshot_hash": before.tree_hash,
                    "summary": "app.py is correct",
                    "evidence": [{"path": "missing.py", "sha256": "0" * 64, "line": 1}]}),
    ):
        with pytest.raises(AutonomyContractError):
            evaluate_stage(context, prose)


def test_text_only_rc0_cannot_satisfy_mutation_implementation(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    context = StageContext.implementation(workspace, before, mutation_required=True)
    with pytest.raises(AutonomyContractError):
        evaluate_stage(context, json.dumps({"kind": "implementation", "summary": "done"}))


def test_implementation_without_source_diff_fails(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    context = StageContext.implementation(workspace, before, mutation_required=True)
    with pytest.raises(AutonomyContractError, match="production source diff"):
        evaluate_stage(context, json.dumps({"kind": "implementation", "summary": "done"}))


def test_changed_protected_tests_or_config_fail(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    (workspace / "app.py").write_text("VALUE = 2\n")
    (workspace / "test_app.py").write_text("def test_value():\n    assert False\n")
    context = StageContext.implementation(workspace, before, mutation_required=True)
    with pytest.raises(AutonomyContractError, match="protected"):
        evaluate_stage(context, json.dumps({"kind": "implementation", "summary": "done"}))


def test_wrong_workspace_identity_fails(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    before = snapshot_workspace(workspace)
    context = StageContext.analysis(workspace, before.tree_hash)
    payload = json.loads(_analysis(workspace, before.tree_hash))
    payload["workspace"] = str(other)
    with pytest.raises(AutonomyContractError, match="workspace"):
        evaluate_stage(context, json.dumps(payload))


def test_writer_outside_access_and_symlink_escape_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AutonomyContractError, match="symlink"):
        WorkspaceBoundary(workspace, writable=True).validate()
    with pytest.raises(AutonomyContractError):
        WorkspaceBoundary(Path("/"), writable=True).validate()


def test_writer_rejects_hardlink_escape(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.link(outside, workspace / "hardlink.py")
    with pytest.raises(AutonomyContractError, match="hardlinked"):
        WorkspaceBoundary(workspace, writable=True).validate()


def test_candidate_is_frozen_and_stale_or_mismatched_evidence_fails(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = CandidateStore(tmp_path / "store")
    frozen = store.freeze(workspace, implementation_run_id="run-freeze")
    (workspace / "app.py").write_text("VALUE = 9\n")
    with store.materialize(frozen) as copy:
        assert snapshot_workspace(copy).tree_hash == frozen.candidate_hash
    bad = frozen.with_hash("f" * 64)
    with pytest.raises(AutonomyContractError, match="candidate hash"):
        with store.materialize(bad):
            pass


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_verifier_os_boundary_blocks_mutation(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = CandidateStore(tmp_path / "store")
    frozen = store.freeze(workspace, implementation_run_id="run-mutation")
    with store.materialize(frozen) as copy:
        result = WorkspaceBoundary(copy, writable=False).run(
            ["/bin/sh", "-c", "echo hacked > app.py"],
            timeout=10,
        )
    assert result.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_mutate_test_restore_attack_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = CandidateStore(tmp_path / "store")
    frozen = store.freeze(workspace, implementation_run_id="run-restore")
    with store.materialize(frozen) as copy:
        result = WorkspaceBoundary(copy, writable=False).run(
            ["/bin/sh", "-c", "old=$(cat app.py); echo hacked > app.py; printf %s \"$old\" > app.py"],
            timeout=10,
        )
    assert result.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_verifier_cannot_create_temporary_candidate_in_candidate_tree(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = CandidateStore(tmp_path / "store")
    frozen = store.freeze(workspace, implementation_run_id="run-create")
    with store.materialize(frozen) as copy:
        result = WorkspaceBoundary(copy, writable=False).run(
            ["/bin/sh", "-c", "mkdir replacement && cp app.py replacement/app.py"], timeout=10
        )
    assert result.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_failed_tests_prevent_verification(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    frozen = CandidateStore(tmp_path / "store").freeze(
        workspace, implementation_run_id="run-failed-tests"
    )
    context = StageContext.verification(
        workspace, frozen, implementer_service="claude", verifier_service="codex",
        test_command=["/bin/sh", "-c", "exit 7"], criteria=["tests pass"],
    )
    with pytest.raises(AutonomyContractError, match="tests failed"):
        evaluate_stage(context, json.dumps({"kind": "verification", "candidate_hash": frozen.candidate_hash,
            "criteria": [{"criterion": "tests pass", "passed": True, "evidence": "test"}]}))


def test_service_inequality_without_candidate_provenance_fails(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    context = StageContext.verification_without_candidate(
        workspace, implementer_service="claude", verifier_service="codex",
        test_command=["/bin/true"], criteria=["tests pass"], baseline_hash=before.tree_hash,
    )
    with pytest.raises(AutonomyContractError, match="candidate"):
        evaluate_stage(context, json.dumps({"kind": "verification"}))


def test_equal_effective_implementer_and_verifier_fails(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    frozen = CandidateStore(tmp_path / "store").freeze(
        workspace, implementation_run_id="run-equality"
    )
    context = StageContext.verification(
        workspace, frozen, implementer_service="codex", verifier_service="codex",
        test_command=["pytest", "-q"], criteria=["tests pass"],
    )
    with pytest.raises(AutonomyContractError, match="independent"):
        evaluate_stage(
            context,
            json.dumps({
                "kind": "verification",
                "candidate_hash": frozen.candidate_hash,
                "criteria": [
                    {"criterion": "tests pass", "passed": True, "evidence": "checked"}
                ],
            }),
        )


def test_one_failed_criterion_blocks_otherwise_passing_verification(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    frozen = CandidateStore(tmp_path / "store").freeze(
        workspace, implementation_run_id="run-criteria"
    )
    context = StageContext.verification(
        workspace, frozen, implementer_service="claude", verifier_service="codex",
        test_command=["pytest", "-q"], criteria=["tests pass", "lint passes"],
    )
    with pytest.raises(AutonomyContractError, match="criterion"):
        evaluate_stage(
            context,
            json.dumps({
                "kind": "verification",
                "candidate_hash": frozen.candidate_hash,
                "criteria": [
                    {"criterion": "tests pass", "passed": True, "evidence": "pytest"},
                    {"criterion": "lint passes", "passed": False, "evidence": "ruff failed"},
                ],
            }),
        )


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_valid_candidate_and_independent_read_only_verification_pass(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    (workspace / "app.py").write_text("VALUE = 2\n")
    impl = evaluate_stage(
        StageContext.implementation(workspace, before, mutation_required=True,
                                    candidate_store=tmp_path / "store",
                                    implementation_run_id="run-valid"),
        json.dumps({"kind": "implementation", "summary": "updated value"}),
    )
    frozen = impl.candidate
    assert frozen is not None
    verified = evaluate_stage(
        StageContext.verification(
            workspace, frozen, implementer_service="claude", verifier_service="codex",
            test_command=["/bin/true"], criteria=["tests pass"],
        ),
        json.dumps({"kind": "verification", "candidate_hash": frozen.candidate_hash,
                    "criteria": [{"criterion": "tests pass", "passed": True,
                                  "evidence": "exit code 0"}]}),
    )
    assert verified.evidence["test_exit_code"] == 0
    assert verified.evidence["read_only_boundary"] == "bubblewrap"


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_real_pytest_runs_on_integrity_bound_disposable_candidate(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = snapshot_workspace(workspace)
    (workspace / "app.py").write_text("VALUE = 2\n")
    impl = evaluate_stage(
        StageContext.implementation(
            workspace, before, mutation_required=True,
            candidate_store=tmp_path / "store",
            implementation_run_id="run-pytest",
        ),
        json.dumps({"kind": "implementation", "summary": "updated value"}),
    )
    frozen = impl.candidate
    assert frozen is not None
    verified = evaluate_stage(
        StageContext.verification(
            workspace, frozen, implementer_service="claude", verifier_service="codex",
            test_command=["pytest", "-q"], criteria=["tests pass"],
        ),
        json.dumps({
            "kind": "verification",
            "candidate_hash": frozen.candidate_hash,
            "criteria": [
                {"criterion": "tests pass", "passed": True, "evidence": "pytest -q"}
            ],
        }),
    )
    assert verified.evidence["test_exit_code"] == 0
    assert "1 passed" in verified.evidence["test_output"]


def test_candidate_provenance_contains_run_and_freeze_timestamp(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    frozen = CandidateStore(tmp_path / "store").freeze(
        workspace, implementation_run_id="run-123"
    )
    assert frozen.implementation_run_id == "run-123"
    assert frozen.candidate_timestamp.endswith("+00:00")


def test_result_must_be_produced_before_verifier_and_bound_to_input(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    input_hash = snapshot_workspace(workspace).tree_hash
    producer = StageContext.result_producer(workspace, input_hash)
    with pytest.raises(AutonomyContractError):
        evaluate_stage(producer, json.dumps({"kind": "analysis", "snapshot_hash": input_hash,
                                             "summary": "not a typed result", "evidence": []}))
    result = evaluate_stage(
        producer,
        json.dumps({"kind": "result", "input_hash": input_hash,
                    "result": {"count": 3}, "derivation": "counted records"}),
    )
    verifier = StageContext.result_verifier(
        workspace, input_hash, producer_service="claude", verifier_service="codex",
        produced_result=result.evidence,
    )
    verified = evaluate_stage(
        verifier,
        json.dumps({"kind": "result_verification", "input_hash": input_hash,
                    "producer_result_hash": result.evidence["result_hash"],
                    "passed": True, "evidence": "independent recomputation"}),
    )
    assert verified.evidence["producer_result_hash"] == result.evidence["result_hash"]


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_boundary_environment_is_allowlisted_and_home_is_hidden(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    os.environ["ANTIGONA_SHOULD_NOT_LEAK"] = "secret"
    result = WorkspaceBoundary(workspace, writable=False).run(
        ["/bin/sh", "-c", "test -z \"$ANTIGONA_SHOULD_NOT_LEAK\" && test \"$HOME\" = /tmp/home"],
        timeout=10,
    )
    assert result.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_explicit_writable_outside_host_path_is_blocked(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside-host.txt"
    result = WorkspaceBoundary(workspace, writable=True).run(
        ["/bin/sh", "-c", f"echo escaped > {outside}"], timeout=10
    )
    assert result.returncode != 0
    assert not outside.exists()


def test_live_antigona_root_and_descendants_are_rejected() -> None:
    code_root = Path(__file__).resolve().parents[2]
    for path in (code_root, code_root / "src", code_root / "tests"):
        with pytest.raises(AutonomyContractError, match="protected"):
            WorkspaceBoundary(path, writable=True).validate()


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_traversal_and_home_config_evidence_roots_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    traversed = workspace / ".." / workspace.name
    with pytest.raises(AutonomyContractError, match="traversal"):
        WorkspaceBoundary(traversed, writable=True).validate()
    # The protected home roots are derived from the single home resolver
    # (ADR-007), never the literal ``/opt/antigona-home``: point ANTIGONA_HOME_DIR at an
    # isolated home so the assertion holds on any host. The directories are
    # materialized because ``validate`` rejects a missing path with the
    # "must be an existing non-root directory" message *before* the protected
    # check — creating them exercises the ``protected`` branch under test.
    monkeypatch.setenv("ANTIGONA_HOME_DIR", str(tmp_path / "isolated_home"))
    config_dir = paths.home_dir() / ".config"
    hermes_prompt_dir = paths.home_dir() / ".hermes" / "master_prompt"
    config_dir.mkdir(parents=True, exist_ok=True)
    hermes_prompt_dir.mkdir(parents=True, exist_ok=True)
    for path in (config_dir, hermes_prompt_dir):
        with pytest.raises(AutonomyContractError, match="protected"):
            WorkspaceBoundary(path, writable=True).validate()


def test_claude_implementation_is_edit_capable_and_workspace_confined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(
        self: WorkspaceBoundary, argv: list[str], *, timeout: int,
        extra_env: dict[str, str] | None = None,
    ) -> BoundaryResult:
        del self, timeout, extra_env
        seen["argv"] = argv
        return BoundaryResult(0, '{"kind":"implementation","summary":"changed"}', "")

    monkeypatch.setattr(WorkspaceBoundary, "run", fake_run)
    result = ServiceExecutors().execute(
        "claude", "edit the source", workspace=str(workspace), writable=True
    )
    assert result.ok is True
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:2] == ["claude", "-p"]
    assert "acceptEdits" in argv
    assert "Edit" in str(argv)
    assert "Do NOT read or write files" not in str(argv)


def test_claude_analysis_has_read_only_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(
        self: WorkspaceBoundary, argv: list[str], *, timeout: int,
        extra_env: dict[str, str] | None = None,
    ) -> BoundaryResult:
        del timeout, extra_env
        seen["writable"] = self.writable
        seen["argv"] = argv
        return BoundaryResult(0, '{"kind":"analysis"}', "")

    monkeypatch.setattr(WorkspaceBoundary, "run", fake_run)
    result = ServiceExecutors().execute(
        "claude", "inspect the source", workspace=str(workspace), writable=False
    )
    assert result.ok is True
    assert seen["writable"] is False
    assert "Read,Glob,Grep" in str(seen["argv"])
    assert "Edit" not in str(seen["argv"])


def test_incapable_direct_mutation_executor_is_non_success(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = ServiceExecutors().execute(
        "grok", "edit the source", workspace=str(workspace), writable=True
    )
    assert result.ok is False
    assert result.failure_class == "CAPABILITY_MISMATCH"


@pytest.mark.skipif(sys.platform == "win32", reason='bubblewrap unavailable on Windows; fail-closed behaviour is correct (Wave 4)')
def test_confined_executor_supplies_empty_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    seen: dict[str, object] = {}

    def fake_subprocess_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    result = WorkspaceBoundary(workspace, writable=False).run(["codex", "exec"], timeout=10)
    assert result.returncode == 0
    assert seen["input"] == ""


@pytest.mark.skipif(sys.platform == "win32", reason="bubblewrap unavailable on Windows")
def test_confined_agy_sandbox_mounts_gemini_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (executor recovery 2026-09-06): confined 'agy' (Antigravity) must get its
    OAuth session under ~/.gemini ro-bound into the sandbox home, mirroring the claude/codex/
    grok credential binds. Without it confined agy exits 1 'authentication required' and the
    executor health row is stuck AUTH_FAILURE/UNAVAILABLE even though host-mode agy works."""
    gemini = paths.home_dir() / ".gemini"
    if not gemini.is_dir():
        pytest.skip(f"{gemini} (Antigravity OAuth) absent on this host")
    workspace = _workspace(tmp_path)
    seen: dict[str, object] = {}

    def fake_subprocess_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = args[0]
        return subprocess.CompletedProcess(args[0], 0, "AGY_OK", "")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    result = WorkspaceBoundary(workspace, writable=False).run(["agy", "-p", "reply ok"], timeout=10)
    assert result.returncode == 0
    cmd = [str(c) for c in seen["cmd"]]
    joined = " ".join(cmd)
    # the confined command must ro-bind the owner's ~/.gemini to /tmp/home/.gemini
    assert str(gemini) in joined
    assert "SANDBOX_HOME" not in joined.replace("--dir /tmp/home/.gemini", "")  # path resolved
