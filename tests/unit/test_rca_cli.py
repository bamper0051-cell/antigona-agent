"""Hermes RCA — CLI overlay + /hermes command wiring tests (spec 10-12, 25)."""

from __future__ import annotations

from antigona.rca.cli import format_result_text, render_overlay
from antigona.rca.result import RCAStatus


class TestCLIOverlay:
    """Spec sections 10, 25 — compact non-blocking panel."""

    def test_overlay_renders_panel(self) -> None:
        panel = render_overlay(
            error="tool execution failed",
            component="Worker",
            task_id="task_821",
            status=RCAStatus.DIAGNOSING.value,
            root_cause="Provider timeout -> lease",
            confidence="HIGH",
            action="/hermes last",
        )
        assert "HERMES RCA" in panel
        assert "STATUS       DIAGNOSING" in panel
        assert "ROOT CAUSE   Provider timeout -> lease" in panel
        assert "ACTION       /hermes last" in panel

    def test_overlay_is_plain_text_no_fullscreen_control(self) -> None:
        panel = render_overlay(error="e", component="c", status=RCAStatus.IDLE.value)
        # must not emit terminal clear / alternate-screen sequences
        assert "\x1b[2J" not in panel
        assert "\x1b[?1049" not in panel

    def test_format_result_text(self) -> None:
        txt = format_result_text(
            {
                "rca_id": "rca_1",
                "error_id": "err_1",
                "category": "PROVIDER",
                "confidence": "HIGH",
                "status": "DIAGNOSED",
                "source_component": "worker",
                "root_cause": "provider stall",
                "user_impact": "task failed",
                "exception_type": "TimeoutError",
                "error_message": "timed out",
                "duplicate_count": 3,
                "recommended_actions": ["retry", "verify key"],
            }
        )
        assert "PROVIDER" in txt
        assert "retry" in txt
        assert "same failure x3" in txt


class TestHermesCommandWiring:
    """/hermes routes to command.hermes and works against real storage."""

    def test_intent_router_maps_hermes(self) -> None:

        from antigona.router.intent_router import IntentRouter

        decision = IntentRouter().route("/hermes last")
        assert decision.intent == "command.hermes"
        assert decision.reason_code == "slash_command_hermes"

    def test_command_registry_contains_hermes(self) -> None:
        from antigona.core.command_registry import commands_for_channel

        specs = commands_for_channel("cli")
        names = {s.name for s in specs}
        assert "hermes" in names

    def test_brain_hermes_last_no_results(self) -> None:
        import asyncio

        from antigona.core.brain import AntigonaBrain

        brain = AntigonaBrain(db_path=":memory:")
        resp = asyncio.run(brain._command_hermes("/hermes last", "s1"))
        assert resp.intent == "command.hermes"
        assert "Hermes RCA" in resp.text or "диагностированных" in resp.text

    def test_format_explain_renders_evidence(self) -> None:
        from antigona.rca.cli import format_explain

        txt = format_explain(
            {
                "rca_id": "rca_9",
                "error_id": "err_9",
                "correlation_id": "cid-9",
                "category": "PROVIDER",
                "confidence": "HIGH",
                "status": "DIAGNOSED",
                "severity": "HIGH",
                "source_component": "worker",
                "tool_name": "llm",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "task_id": "t1",
                "flow_id": "f1",
                "step_id": "s1",
                "exception_type": "TimeoutError",
                "error_message": "timed out",
                "root_cause": "provider stall",
                "user_impact": "task failed",
                "summary": "provider failure",
                "git_revision": "abc123",
                "duplicate_count": 17,
                "recommended_actions": ["retry"],
                "evidence": [{"kind": "exception_type", "value": "TimeoutError"}],
            }
        )
        assert "детальный разбор" in txt
        assert "evidence:" in txt
        assert "exception_type: TimeoutError" in txt
        assert "same failure x17" in txt
        assert "provider stall" in txt


class TestExplainCommand:
    """Section 12 — /hermes explain <error_id> fetches a stored RCA record."""

    def test_explain_missing_error_id(self) -> None:
        import asyncio
        import tempfile

        from antigona.core.brain import AntigonaBrain

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            brain = AntigonaBrain(db_path=f"sqlite:///{tmp}/brain.db")
            resp = asyncio.run(brain._command_hermes("/hermes explain nope", "s1"))
            assert "не найдена" in resp.text

    def test_explain_found_error_id(self) -> None:
        import asyncio
        import tempfile

        from antigona.core.brain import AntigonaBrain
        from antigona.rca import ErrorEnvelope, RCAEngine
        from antigona.rca.dedup import fingerprint
        from antigona.rca.storage import get_repository

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            brain = AntigonaBrain(db_path=f"sqlite:///{tmp}/brain.db")
            env = ErrorEnvelope.from_exception(
                TimeoutError("provider timed out"),
                source_component="worker",
                operation="op",
                correlation_id="cid-explain",
            )
            result = RCAEngine().diagnose(env)
            repo = get_repository(db_path=f"sqlite:///{tmp}/brain.db")
            repo.save(result, env, fingerprint(env), duplicate_count=1)
            resp = asyncio.run(brain._command_hermes(f"/hermes explain {env.error_id}", "s1"))
            assert "детальный разбор" in resp.text
            assert env.error_id in resp.text

    def test_format_evidence_renders_chain(self) -> None:
        from antigona.rca.cli import format_evidence

        txt = format_evidence(
            {
                "rca_id": "rca_5",
                "error_id": "err_5",
                "correlation_id": "cid-5",
                "category": "TOOL",
                "confidence": "HIGH",
                "evidence": [
                    {"kind": "exception_type", "value": "RuntimeError"},
                    {"kind": "tool", "value": "owner_shell"},
                    {"kind": "policy", "value": "denied"},
                ],
            }
        )
        assert "evidence" in txt
        assert "exception_type: RuntimeError" in txt
        assert "tool: owner_shell" in txt


class TestEvidenceCommand:
    """Section 12 — /hermes evidence <rca_id> shows the evidence chain."""

    def test_evidence_missing_rca_id(self) -> None:
        import asyncio
        import tempfile

        from antigona.core.brain import AntigonaBrain

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            brain = AntigonaBrain(db_path=f"sqlite:///{tmp}/brain.db")
            resp = asyncio.run(brain._command_hermes("/hermes evidence nope", "s1"))
            assert "не найден" in resp.text

    def test_evidence_found_rca_id(self) -> None:
        import asyncio
        import tempfile

        from antigona.core.brain import AntigonaBrain
        from antigona.rca import ErrorEnvelope, RCAEngine
        from antigona.rca.dedup import fingerprint
        from antigona.rca.storage import get_repository

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            brain = AntigonaBrain(db_path=f"sqlite:///{tmp}/brain.db")
            env = ErrorEnvelope.from_exception(
                RuntimeError("tool failed"),
                source_component="worker",
                operation="op",
                correlation_id="cid-ev",
                tool_name="owner_shell",
            )
            result = RCAEngine().diagnose(env)
            repo = get_repository(db_path=f"sqlite:///{tmp}/brain.db")
            repo.save(result, env, fingerprint(env), duplicate_count=1)
            resp = asyncio.run(brain._command_hermes(f"/hermes evidence {result.rca_id}", "s1"))
            assert "evidence" in resp.text
            assert result.rca_id in resp.text

    def test_format_suggest_fix_renders_actions_and_safety(self) -> None:
        from antigona.rca.cli import format_suggest_fix

        txt = format_suggest_fix(
            {
                "rca_id": "rca_6",
                "error_id": "err_6",
                "correlation_id": "cid-6",
                "category": "PROVIDER",
                "confidence": "HIGH",
                "root_cause": "provider stall",
                "user_impact": "task failed",
                "recommended_actions": ["retry with backoff", "verify key"],
                "safe_to_auto_fix": False,
                "requires_owner_approval": True,
            }
        )
        assert "suggested fix" in txt
        assert "retry with backoff" in txt
        assert "safe_to_auto_fix        False" in txt
        assert "requires_owner_approval True" in txt


class TestSuggestFixCommand:
    """Section 12 — /hermes suggest-fix <rca_id> shows remediation (data only)."""

    def test_suggest_fix_missing_rca_id(self) -> None:
        import asyncio
        import tempfile

        from antigona.core.brain import AntigonaBrain

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            brain = AntigonaBrain(db_path=f"sqlite:///{tmp}/brain.db")
            resp = asyncio.run(brain._command_hermes("/hermes suggest-fix nope", "s1"))
            assert "не найден" in resp.text

    def test_suggest_fix_found_rca_id_is_read_only(self) -> None:
        import asyncio
        import tempfile

        from antigona.core.brain import AntigonaBrain
        from antigona.rca import ErrorEnvelope, RCAEngine
        from antigona.rca.dedup import fingerprint
        from antigona.rca.storage import get_repository

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            brain = AntigonaBrain(db_path=f"sqlite:///{tmp}/brain.db")
            env = ErrorEnvelope.from_exception(
                TimeoutError("provider timed out"),
                source_component="worker",
                operation="op",
                correlation_id="cid-fix",
                provider="deepseek",
            )
            result = RCAEngine().diagnose(env)
            repo = get_repository(db_path=f"sqlite:///{tmp}/brain.db")
            repo.save(result, env, fingerprint(env), duplicate_count=1)
            resp = asyncio.run(brain._command_hermes(f"/hermes suggest-fix {result.rca_id}", "s1"))
            assert "suggested fix" in resp.text
            assert "Hermes does not apply changes" in resp.text
            assert result.rca_id in resp.text

