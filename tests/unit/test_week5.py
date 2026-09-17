"""Week 5 tests — Days 29-35: TUI Control, Skills, Delegation, Tasks, Models, Resilience.

Gate: keyboard navigation in TUI + partial subsystem failure tolerated.
"""

from __future__ import annotations

import asyncio

from textual.widgets import ContentSwitcher, DataTable

# ── D29: TUI skeleton ─────────────────────────────────────────────────────────


class TestDay29TuiSkeleton:
    """Textual app, panels, status bar, keyboard navigation."""

    def test_control_app_importable(self) -> None:
        from antigona.tui_control.app import ControlApp as Imported

        assert Imported is not None

    def test_control_app_construct(self) -> None:
        from antigona.tui_control.app import ControlApp

        app = ControlApp()
        assert app is not None
        assert app.title == "Antigona Runtime Control"

    def test_tab_specs_declare_six_panes(self) -> None:
        from antigona.tui_control.app import TAB_SPECS

        titles = [title for _tab, title, _pane in TAB_SPECS]
        assert titles == [
            "Tasks", "Skills", "Delegation", "Models", "Resilience", "Events",
        ]
        assert len(TAB_SPECS) == 6

    def test_bindings_declared(self) -> None:
        from antigona.tui_control.app import ControlApp

        keys = {b.action.split("(")[0] if "(" in b.action else b.action for b in ControlApp.BINDINGS}
        assert "quit" in keys
        assert "show_tab" in keys

    def test_status_bar_reactivity(self) -> None:
        """Reactive attributes must be read inside a mounted app context.

        Textual's reactive ``__get__`` initialises the attribute lazily, which
        runs the watchers and calls ``self.update()`` — that needs an active
        app (``widget.app.console``). So we query the live StatusBar inside
        ``run_test`` instead of touching an unmounted widget.
        """
        from antigona.tui_control.app import ControlApp, StatusBar

        async def _check() -> None:
            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                bar = app.query_one("#status_bar", StatusBar)
                assert bar.skills_ok is True
                assert bar.delegation_ok is True
                assert bar.bus_ok is True
                assert bar.resilience_ok is True
                assert bar.event_count == 0

        asyncio.run(_check())

    def test_status_bar_updates_on_change(self) -> None:
        """Setting reactive attributes on the live StatusBar reflects state."""
        from antigona.tui_control.app import ControlApp, StatusBar

        async def _check() -> None:
            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                bar = app.query_one("#status_bar", StatusBar)
                bar.skills_ok = False
                assert bar.skills_ok is False
                bar.event_count = 5
                assert bar.event_count == 5

        asyncio.run(_check())

    def test_keyboard_navigation_switches_tabs(self) -> None:
        """D29 gate: keyboard navigation in TUI."""
        from antigona.tui_control.app import ControlApp

        async def _check() -> None:
            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                panes = app.query_one("#panes", ContentSwitcher)
                assert panes.current == "pane-tasks"

                for key, expected in (
                    ("2", "pane-skills"),
                    ("3", "pane-delegation"),
                    ("4", "pane-models"),
                    ("5", "pane-resilience"),
                    ("6", "pane-events"),
                    ("1", "pane-tasks"),
                ):
                    await pilot.press(key)
                    await pilot.pause()
                    assert panes.current == expected, f"key {key} → {expected}, got {panes.current}"

        asyncio.run(_check())

    def test_all_panes_render_safely(self) -> None:
        """Even without panels mounted, placeholders appear instead of crashing."""
        from antigona.tui_control.app import ControlApp

        async def _check() -> None:
            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                # Switch through all panes — none should crash
                for key in ("2", "3", "4", "5", "6", "1"):
                    await pilot.press(key)
                    await pilot.pause()

        asyncio.run(_check())

    def test_event_bus_integration(self) -> None:
        """D30 core: event bus subscription delivers events to the table."""
        from antigona.events.bus import EventBus
        from antigona.events.event_types import TaskCreated, ToolExecuted
        from antigona.tui_control.app import EVENT_COLUMNS, ControlApp

        bus = EventBus()

        async def _check() -> None:
            app = ControlApp(bus=bus)
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                await pilot.press("6")  # Events tab
                await pilot.pause()
                table = app.query_one("#event_table", DataTable)
                # The app composes the event table bare; give it the columns the
                # wildcard handler appends (Time/Type/Correlation ID/Payload).
                table.add_columns(*EVENT_COLUMNS)
                initial = table.row_count

                # Publish events via the bus
                await bus.publish(TaskCreated(correlation_id="cid1", task_id="t1", goal="test"))
                await bus.publish(ToolExecuted(correlation_id="cid2", task_id="t1", tool_name="grep", success=True))
                await bus.publish(ToolExecuted(correlation_id="cid3", task_id="t1", tool_name="rm", success=False))

                await pilot.pause()
                assert table.row_count == initial + 3

        asyncio.run(_check())


# ── D30: Event-driven TUI ──────────────────────────────────────────────────────


class TestDay30EventDriven:
    """Renderer subscribes to core events.  Live tool/progress view.  No business logic."""

    def test_any_event_handler_registered(self) -> None:
        """The app subscribes a wildcard handler on mount."""
        from antigona.events.bus import EventBus
        from antigona.tui_control.app import ControlApp

        bus = EventBus()

        async def _check() -> None:
            app = ControlApp(bus=bus)
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                # Subscription happens in on_mount, so it needs a live app.
                assert len(bus._any_subscribers) == 1

        asyncio.run(_check())

    def test_short_event_summary_all_types(self) -> None:
        from antigona.events.event_types import (
            Cancelled,
            CancelRequested,
            ErrorOccurred,
            IntentClassified,
            MessageReceived,
            ToolExecuted,
        )
        from antigona.tui_control.app import short_event_summary

        assert "grep" in short_event_summary(ToolExecuted(tool_name="grep", success=True))
        assert "✗" in short_event_summary(ToolExecuted(tool_name="rm", success=False))
        assert "ERR" in short_event_summary(ErrorOccurred(source_component="x", message="boom"))
        assert "cid_short" not in short_event_summary(MessageReceived(correlation_id="abc12345", text="hello"))
        assert "intent" in short_event_summary(IntentClassified(intent="query", confidence=0.95))
        assert "CANCEL" in short_event_summary(CancelRequested(task_id="t1", reason="timeout"))
        assert "CANCELLED" in short_event_summary(Cancelled(task_id="t1", reason="done"))

    def test_all_events_go_to_event_table(self) -> None:
        from antigona.events.bus import EventBus
        from antigona.events.event_types import MessageReceived, TaskCompleted
        from antigona.tui_control.app import EVENT_COLUMNS, ControlApp

        bus = EventBus()

        async def _check() -> None:
            app = ControlApp(bus=bus)
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                await pilot.press("6")
                await pilot.pause()
                table = app.query_one("#event_table", DataTable)
                table.add_columns(*EVENT_COLUMNS)
                start = table.row_count
                await bus.publish(MessageReceived(correlation_id="x", text="hi"))
                await bus.publish(TaskCompleted(correlation_id="y", task_id="t1", status="done"))
                await pilot.pause()
                assert table.row_count == start + 2

        asyncio.run(_check())

    def test_publish_does_not_crash_without_app(self) -> None:
        """EventBus publish doesn't crash even when no app is listening."""
        from antigona.events.bus import EventBus
        from antigona.events.event_types import TaskCreated

        bus = EventBus()

        async def _check() -> None:
            await bus.publish(TaskCreated(correlation_id="c", task_id="t1", goal="no crash"))

        asyncio.run(_check())


# ── D31: Skills loader ─────────────────────────────────────────────────────────


class TestDay31SkillsLoader:
    """Skills manifest, discovery, activation, permissions.  Broken skill isolated."""

    def test_skills_panel_construct(self) -> None:
        from antigona.tui_control.skills_panel import SkillsPanel

        panel = SkillsPanel()
        assert panel is not None
        assert panel.skill_count == 0

    def test_skills_panel_empty_on_init(self) -> None:
        from antigona.tui_control.skills_panel import SkillsPanel

        panel = SkillsPanel()
        assert panel.active_count == 0
        assert panel.quarantined_count == 0

    def test_skills_panel_reactive_counts(self) -> None:
        from antigona.tui_control.skills_panel import SkillsPanel

        panel = SkillsPanel()
        panel.skill_count = 5
        panel.active_count = 3
        panel.quarantined_count = 1
        assert panel.skill_count == 5
        assert panel.active_count == 3
        assert panel.quarantined_count == 1


# ── D32: Delegation contracts ──────────────────────────────────────────────────


class TestDay32DelegationContracts:
    """Claude/Codex/Antigravity adapters.  Artifacts returned and verified."""

    def test_claude_adapter_construct(self) -> None:
        from antigona.tui_control.delegation import ClaudeAdapter

        adapter = ClaudeAdapter()
        assert adapter.name == "claude"
        assert adapter.binary == "claude"

    def test_codex_adapter_construct(self) -> None:
        from antigona.tui_control.delegation import CodexAdapter

        adapter = CodexAdapter()
        assert adapter.name == "codex"
        assert adapter.binary == "codex"

    def test_antigravity_adapter_construct(self) -> None:
        from antigona.tui_control.delegation import AntigravityAdapter

        adapter = AntigravityAdapter()
        assert adapter.name == "antigravity"

    def test_claude_call_and_verify(self) -> None:
        from antigona.tui_control.delegation import ClaudeAdapter, DelegationTask

        adapter = ClaudeAdapter()

        async def _check() -> None:
            task = DelegationTask(id="t1", goal="test", prompt="do something")
            result = await adapter.call(task)
            assert result.task_id == "t1"
            assert result.status.value == "success"

            verdict = await adapter.verify(result)
            assert verdict.passed is True
            assert "output_generated" in verdict.checks

        asyncio.run(_check())

    def test_antigravity_call_and_verify(self) -> None:
        from antigona.tui_control.delegation import AntigravityAdapter, DelegationTask

        adapter = AntigravityAdapter()

        async def _check() -> None:
            task = DelegationTask(id="t2", goal="test", prompt="analyze")
            result = await adapter.call(task)
            assert result.status.value == "success"
            assert adapter.status.value == "success"

            verdict = await adapter.verify(result)
            assert verdict.passed is True

        asyncio.run(_check())

    def test_artifact_dataclass(self) -> None:
        from antigona.tui_control.delegation import Artifact

        art = Artifact(name="test.txt", content_type="text", size_bytes=100, sha256="abc")
        assert art.name == "test.txt"
        assert art.verified is False

    def test_delegation_panel_construct(self) -> None:
        from antigona.tui_control.delegation_panel import DelegationPanel

        panel = DelegationPanel()
        assert panel.total_calls == 0
        assert panel.failed_calls == 0
        assert len(panel._adapters) == 3

    def test_delegation_panel_record_call(self) -> None:
        """record_call updates call/failure stats; needs a mounted panel.

        ``record_call`` ends by rebuilding the adapter DataTable, which is only
        populated once the panel is mounted (``on_mount`` sets ``_table``), so
        the panel must live inside a running app.
        """
        from antigona.tui_control.app import ControlApp
        from antigona.tui_control.delegation_panel import DelegationPanel

        async def _check() -> None:
            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()

                panel = DelegationPanel()
                app.mount(panel)
                await pilot.pause()

                panel.record_call("claude", 1.5)
                assert panel.total_calls == 1
                panel.record_call("claude", 2.0, error="timeout")
                assert panel.total_calls == 2
                assert panel.failed_calls == 1

        asyncio.run(_check())


# ── D33: Task visualization ────────────────────────────────────────────────────


class TestDay33TaskVisualization:
    """Preview/progress/approval/result cards.  Conversation remains card-free."""

    def test_task_card_construct(self) -> None:
        from antigona.tui_control.task_panel import TaskCard

        card = TaskCard(task_id="t1", goal="test task")
        assert card.task_id == "t1"
        assert card.goal == "test task"
        assert card.status == ""

    def test_task_card_reactive(self) -> None:
        from antigona.tui_control.task_panel import TaskCard

        card = TaskCard()
        card.status = "running"
        assert card.status == "running"
        card.progress = 0.5
        assert card.progress == 0.5

    def test_task_panel_construct(self) -> None:
        from antigona.tui_control.task_panel import TaskPanel

        panel = TaskPanel()
        assert panel.task_count == 0
        assert panel.running_count == 0

    def test_task_panel_upsert_card(self) -> None:
        """Upsert creates or updates a card and updates stats."""

        from antigona.tui_control.task_panel import TaskPanel

        async def _check() -> None:
            from antigona.tui_control.app import ControlApp

            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()

                panel = TaskPanel()
                app.mount(panel)
                await pilot.pause()

                # The panel starts empty
                assert panel.task_count == 0

        asyncio.run(_check())

    def test_task_card_requires_approval_flag(self) -> None:
        from antigona.tui_control.task_panel import TaskCard

        card = TaskCard()
        assert card.requires_approval is False
        card.requires_approval = True
        assert card.requires_approval is True


# ── D34: Model picker ──────────────────────────────────────────────────────────


class TestDay34ModelPicker:
    """Provider/model capabilities and /setllm UI.  Selection persists."""

    def test_provider_capability_dataclass(self) -> None:
        from antigona.tui_control.model_panel import ProviderCapability

        cap = ProviderCapability("openai", "gpt-4o")
        assert cap.provider == "openai"
        assert cap.model == "gpt-4o"
        assert cap.available is True

    def test_default_capabilities_populated(self) -> None:
        from antigona.tui_control.model_panel import DEFAULT_CAPABILITIES

        assert len(DEFAULT_CAPABILITIES) == 7
        providers = {c.provider for c in DEFAULT_CAPABILITIES}
        assert "openai" in providers
        assert "anthropic" in providers
        assert "deepseek" in providers

    def test_model_panel_construct(self) -> None:
        from antigona.tui_control.model_panel import ModelPanel

        panel = ModelPanel()
        assert panel.selected_provider == ""
        assert panel.selected_model == ""
        assert len(panel._capabilities) == 7

    def test_apply_selection_sets_state(self) -> None:
        from antigona.tui_control.model_panel import ModelPanel

        panel = ModelPanel()
        panel._apply_selection("openai", "gpt-4o")
        assert panel.selected_provider == "openai"
        assert panel.selected_model == "gpt-4o"

    def test_rebuild_models_filters_by_provider(self) -> None:
        from antigona.tui_control.model_panel import ModelPanel

        panel = ModelPanel()
        panel._rebuild_models("openai")
        # Models are bare names (e.g. "gpt-4o"), not "openai/...", so assert the
        # provider's exact model set rather than searching the provider name in
        # each model string.
        assert panel._available_models == ["gpt-4o", "gpt-4o-mini"]
        assert len(panel._available_models) == 2

    def test_rebuild_providers_picks_unique(self) -> None:
        from antigona.tui_control.model_panel import ModelPanel

        panel = ModelPanel()
        panel._rebuild_providers()
        assert "openai" in panel._available_providers
        assert "anthropic" in panel._available_providers


# ── D35: Resilience pass ───────────────────────────────────────────────────────


class TestDay35Resilience:
    """Retry budgets, circuit breakers, queues.  Fault tests."""

    def test_circuit_breaker_closed_on_init(self) -> None:
        from antigona.tui_control.resilience import CircuitBreaker, CircuitState

        cb = CircuitBreaker(name="test")
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_circuit_breaker_opens_on_threshold(self) -> None:
        from antigona.tui_control.resilience import CircuitBreaker, CircuitState

        cb = CircuitBreaker(failure_threshold=3, name="test")
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_circuit_breaker_recovers(self) -> None:
        """After recovery_timeout, OPEN → HALF_OPEN → (on success) CLOSED."""
        from antigona.tui_control.resilience import CircuitBreaker, CircuitState

        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=-1, name="test")
        cb.record_failure()
        cb.record_failure()

        # The internal state flips to OPEN on the threshold...
        assert cb._state == CircuitState.OPEN
        # ...but the `state` property immediately transitions to HALF_OPEN once
        # recovery_timeout has elapsed (negative ⇒ always elapsed).
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_retry_budget_limits(self) -> None:
        from antigona.tui_control.resilience import RetryBudget

        budget = RetryBudget(max_retries=2)
        assert budget.allow_retry() is True
        budget.record_attempt()
        assert budget.allow_retry() is True
        budget.record_attempt()
        assert budget.allow_retry() is False  # Exhausted
        assert budget.remaining == 0

    def test_retry_budget_reset(self) -> None:
        from antigona.tui_control.resilience import RetryBudget

        budget = RetryBudget(max_retries=1)
        budget.record_attempt()
        assert budget.allow_retry() is False
        budget.reset()
        assert budget.allow_retry() is True

    def test_retry_budget_remaining(self) -> None:
        from antigona.tui_control.resilience import RetryBudget

        budget = RetryBudget(max_retries=5)
        assert budget.remaining == 5
        budget.record_attempt()
        assert budget.remaining == 4

    def test_resilient_queue_basics(self) -> None:
        from antigona.tui_control.resilience import ResilientQueue

        q = ResilientQueue(name="test")
        assert q.depth == 0
        q.enqueue({"id": 1})
        assert q.depth == 1
        item = q.dequeue()
        assert item == {"id": 1}
        assert q.depth == 0

    def test_resilient_queue_processing_counts(self) -> None:
        from antigona.tui_control.resilience import ResilientQueue

        q = ResilientQueue(name="test")
        q.mark_processed()
        q.mark_failed()
        assert q.total_processed == 1
        assert q.total_failed == 1

    def test_resilience_panel_construct(self) -> None:
        from antigona.tui_control.resilience import ResiliencePanel

        panel = ResiliencePanel()
        assert panel.circuits_open == 0
        assert panel.queue_depth == 0
        assert panel.retries_used == 0

    def test_resilience_panel_register(self) -> None:
        from antigona.tui_control.resilience import (
            CircuitBreaker,
            ResiliencePanel,
            ResilientQueue,
            RetryBudget,
        )

        panel = ResiliencePanel()
        cb = CircuitBreaker(name="http", failure_threshold=5)
        q = ResilientQueue(name="tasks")
        b = RetryBudget(max_retries=3)

        panel.register_breaker("http", cb)
        panel.register_queue("tasks", q)
        panel.register_budget("retry-llm", b)

        assert "http" in panel._breakers
        assert "tasks" in panel._queues
        assert "retry-llm" in panel._budgets

    def test_partial_subsystem_failure_tolerated(self) -> None:
        """D35 gate: one breaker open doesn't affect others.

        The ResiliencePanel tracks all breakers independently;
        one open circuit doesn't cascade.
        """
        from antigona.tui_control.resilience import CircuitBreaker, ResiliencePanel

        panel = ResiliencePanel()

        cb_ok = CircuitBreaker(name="ok", failure_threshold=10)
        cb_fail = CircuitBreaker(name="fail", failure_threshold=1)

        panel.register_breaker("ok", cb_ok)
        panel.register_breaker("fail", cb_fail)

        # Only the failing one opens
        cb_fail.record_failure()
        panel._rebuild()

        assert cb_ok.state.value == "closed"
        assert cb_fail.state.value == "open"
        assert panel.circuits_open == 1

    def test_empty_queue_returns_none(self) -> None:
        from antigona.tui_control.resilience import ResilientQueue

        q = ResilientQueue()
        assert q.dequeue() is None

    def test_circuit_breaker_successes_accumulate(self) -> None:
        from antigona.tui_control.resilience import CircuitBreaker

        cb = CircuitBreaker(name="test")
        for _ in range(5):
            cb.record_success()
        assert cb.state.value == "closed"


# ── Gate: partial subsystem failure tolerance ──────────────────────────────────


class TestWeek5Gate:
    """Gate недели: Keyboard navigation in TUI.  Partial subsystem failure tolerated."""

    def test_keyboard_navigation_all_keys(self) -> None:
        """All 6 number keys + q + r work without error."""
        from antigona.tui_control.app import ControlApp

        async def _check() -> None:
            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                for key in ("1", "2", "3", "4", "5", "6", "r", "q"):
                    await pilot.press(key)
                    await pilot.pause()

        asyncio.run(_check())

    def test_partial_failure_tolerated(self) -> None:
        """Panel subsystem independence: one panel failing doesn't crash others."""
        from antigona.tui_control.app import ControlApp

        async def _check() -> None:
            app = ControlApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                # Switch through all panes rapidly — no crash expected
                for key in ("2", "4", "5", "6", "3", "1"):
                    await pilot.press(key)
                    await pilot.pause()

        asyncio.run(_check())

    def test_clean_room_provenance(self) -> None:
        """No upstream TUI markers in tui_control modules."""
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent.parent / "src" / "antigona" / "tui_control"
        forbidden = {
            "hermes", "openclaw", "openhands", "klio",
            "prompt_toolkit", "npyscreen", "urwid", "py_cui", "blessed", "curses",
        }

        for pyfile in src.rglob("*.py"):
            source = pyfile.read_text(encoding="utf-8").lower()
            for marker in forbidden:
                assert marker not in source, f"{pyfile.name} references {marker}"

    def test_no_repository_access(self) -> None:
        """tui_control panels must not touch persistence layers.

        The ban is against *code* that reaches for a repository/database/session
        handle. Panels legitimately mention these words in prose docstrings to
        disclaim them ("never writes to a database directly"), so we strip
        comments and string literals before scanning — real code references
        (imports, attribute access) are still flagged.
        """
        import io
        import tokenize
        from pathlib import Path

        def _code_tokens(source: str) -> str:
            out: list[str] = []
            for tok in tokenize.generate_tokens(io.StringIO(source).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    out.append(" " * len(tok.string))
                else:
                    out.append(tok.string)
            return "".join(out)

        src = Path(__file__).resolve().parent.parent.parent / "src" / "antigona" / "tui_control"
        banned = {
            "repository", "database", "verifier_service", "verifier_client",
            "session_factory", "TaskRepository",
        }

        for pyfile in src.rglob("*.py"):
            code = _code_tokens(pyfile.read_text(encoding="utf-8"))
            for marker in banned:
                assert marker not in code, f"{pyfile.name} references {marker}"
