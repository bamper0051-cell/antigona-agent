"""Unit tests for the Textual TUI client and the collection reads it drives.

The widget-level assertions run the app through Textual's headless test mode
(``run_test``) inside ``asyncio.run``; the projection helpers are pure and are
asserted directly, without a running app.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx
import pytest
from textual.widgets import ContentSwitcher, DataTable, Input, Tab, Tabs, Tree

from antigona.cli import GatewayClient
from antigona.tui import (
    TAB_SPECS,
    AntigonaApp,
    approval_rows,
    artifact_rows,
    flow_rows,
    flow_summary_text,
    risk_color,
    status_color,
    step_labels,
    transition_row,
    transition_rows,
)

SAMPLE_REPLAY: dict[str, Any] = {
    "task_id": "b3c4",
    "goal": "Проверить артефакт sha256",
    "status": "WAITING_APPROVAL",
    "transitions": [
        {
            "id": 1, "entity_id": "b3c4", "entity_type": "task",
            "from_state": None, "to_state": "RECEIVED",
            "reason": "accepted", "actor": "gateway",
            "created_at": "2026-07-26T10:02:00",
        },
        {
            "id": 2, "entity_id": "b3c4", "entity_type": "task",
            "from_state": "RECEIVED", "to_state": "QUEUED",
            "reason": "enqueued", "actor": "gateway",
            "created_at": "2026-07-26T10:02:01",
        },
        {
            "id": 3, "entity_id": "b3c4", "entity_type": "task",
            "from_state": "PLANNING", "to_state": "WAITING_APPROVAL",
            "reason": "risk HIGH -> approval", "actor": "worker",
            "created_at": "2026-07-26T10:02:05",
        },
    ],
    "steps": [
        {
            "id": "s1", "index": 0, "title": "Execute verify", "status": "COMPLETED",
            "input": {"path": "index.html"}, "output": {"ok": True}, "retries": 0,
        },
        {
            "id": "s2", "index": 1, "title": "Capture sha256", "status": "PENDING",
            "input": {"path": "index.html"}, "output": None, "retries": 1,
        },
    ],
    "artifacts": [
        {
            "id": "art-4d8c", "step_id": "s1", "path": "index.html",
            "sha256": "a" * 64, "size": 42, "verified": True,
        },
    ],
}


class RecordingClient(GatewayClient):
    """GatewayClient stand-in that records calls instead of doing network IO."""

    def __init__(self) -> None:
        super().__init__("http://test", "test-token")
        self.decisions: list[tuple[str, bool]] = []
        self.cancelled: list[str] = []

    async def list_flows(
        self, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "f1a2", "goal": "Сгенерировать health-report",
                    "status": "WAITING_APPROVAL", "revision": 3,
                    "created_at": "2026-07-26T10:02:00", "updated_at": "2026-07-26T10:02:05",
                }
            ],
            "total": 1,
        }

    async def list_approvals(
        self, status: str = "PENDING", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "ap-01", "task_id": "f1a2", "tool_name": "sandbox.shell",
                    "risk_level": "HIGH", "reason": "executes rm -rf",
                    "created_at": "2026-07-26T10:02:05",
                }
            ],
            "total": 1,
        }

    async def decide_approval(self, approval_id: str, approve: bool) -> dict[str, Any]:
        self.decisions.append((approval_id, approve))
        return {"id": approval_id, "decision": "APPROVED" if approve else "DENIED"}

    async def get_replay(
        self,
        flow_id: str,
        actor: str | None = None,
        entity_type: str | None = None,
        from_dt: str | None = None,
        to_dt: str | None = None,
    ) -> dict[str, Any]:
        return SAMPLE_REPLAY

    async def get_flow(self, flow_id: str) -> dict[str, Any]:
        return {"id": flow_id, "goal": "demo", "status": "QUEUED", "revision": 1}

    async def cancel_flow(self, flow_id: str) -> dict[str, Any]:
        self.cancelled.append(flow_id)
        return {"id": flow_id, "status": "CANCELLED"}


def _mock_transport(
    payload: dict[str, Any],
) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return seen, httpx.MockTransport(handler)


# ── Import / construction ────────────────────────────────────────────


def test_app_importable() -> None:
    """Importing the module must not start an app or touch the network."""
    from antigona.tui import AntigonaApp as Imported

    assert Imported is AntigonaApp
    import antigona.tui as tui_module

    assert tui_module.__all__ == ["AntigonaApp", "main"]


def test_tui_app_construct() -> None:
    app = AntigonaApp(gateway_url="http://localhost:8000", token="test-token")
    assert app is not None
    assert app.gateway_url == "http://localhost:8000"
    assert app.token == "test-token"
    assert app.title == "Antigona TUI Client"
    assert app.attached_flow_id is None


def test_tab_specs_declare_four_panes() -> None:
    assert [title for _tab, title, _pane in TAB_SPECS] == [
        "Flows", "Live", "Approvals", "Replay",
    ]


def test_compose_builds_tabs() -> None:
    """The composed DOM carries exactly the four documented tabs."""

    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            labels = [str(tab.label) for tab in app.query_one("#main_tabs", Tabs).query(Tab)]
            assert labels == ["Flows", "Live", "Approvals", "Replay"]
            switcher = app.query_one("#panes", ContentSwitcher)
            assert switcher.current == "pane-flows"

    asyncio.run(_check())


# ── GatewayClient collection reads ───────────────────────────────────


def test_gatewayclient_list_flows_params() -> None:
    seen, transport = _mock_transport({"items": [], "total": 0})
    client = GatewayClient("http://gw.test", "tok", transport=transport)
    result = asyncio.run(client.list_flows(status="DONE", limit=10, offset=5))
    # list_flows returns a plain list of FlowView, not the envelope dict.
    assert result == []
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/flows"
    assert dict(request.url.params) == {"limit": "10", "offset": "5", "status": "DONE"}
    assert request.headers["Authorization"] == "Bearer tok"


def test_gatewayclient_list_flows_omits_empty_status() -> None:
    seen, transport = _mock_transport({"items": [], "total": 0})
    client = GatewayClient("http://gw.test", "tok", transport=transport)
    asyncio.run(client.list_flows())
    assert "status" not in dict(seen[0].url.params)


def test_gatewayclient_list_approvals_params() -> None:
    seen, transport = _mock_transport({"items": [], "total": 0})
    client = GatewayClient("http://gw.test", "tok", transport=transport)
    asyncio.run(client.list_approvals())
    request = seen[0]
    assert request.url.path == "/approvals"
    assert dict(request.url.params)["status"] == "PENDING"


def test_gatewayclient_get_approval_params() -> None:
    # get_approval parses/validates a full approval body and returns an
    # ApprovalView object, so the mock must carry every required field.
    seen, transport = _mock_transport({
        "id": "ap-01",
        "decision": "PENDING",
        "tool_name": "sandbox.shell",
        "risk_level": "HIGH",
        "reason": "executes rm -rf",
    })
    client = GatewayClient("http://gw.test", "tok", transport=transport)
    result = asyncio.run(client.get_approval("ap-01"))
    assert result.id == "ap-01"
    assert result.decision == "PENDING"
    assert seen[0].url.path == "/approvals/ap-01"


def test_progress_ws_url_switches_scheme() -> None:
    assert GatewayClient("https://gw.test", "tok").progress_ws_url("f1").startswith(
        "wss://gw.test/flows/f1/progress?token=tok"
    )
    assert GatewayClient("http://gw.test", "tok").progress_ws_url("f1").startswith("ws://")


def test_gatewayclient_stream_progress_ws_yields() -> None:
    """A mock WS server sends two frames; the generator yields both, then stops."""

    async def _collect() -> list[dict[str, Any]]:
        from websockets.asyncio.server import ServerConnection, serve

        async def handler(connection: ServerConnection) -> None:
            await connection.send(json.dumps({
                "type": "transition", "from_state": "QUEUED", "to_state": "PLANNING",
                "actor": "worker", "reason": "started",
                "created_at": "2026-07-26T10:02:02",
            }))
            await connection.send(json.dumps({"type": "end", "status": "CANCELLED"}))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = int(server.sockets[0].getsockname()[1])
            client = GatewayClient(f"http://127.0.0.1:{port}", "tok")
            return [message async for message in client.stream_progress_ws("flow-1")]

    messages = asyncio.run(_collect())
    assert len(messages) == 2
    assert messages[0]["type"] == "transition"
    assert messages[0]["to_state"] == "PLANNING"
    assert messages[1]["type"] == "end"


# ── Pure projection helpers ──────────────────────────────────────────


def test_status_color_mapping() -> None:
    assert status_color("DONE") == "green"
    assert status_color("done") == "green"
    assert status_color("FAILED") == "red"
    assert status_color("BLOCKED") == "red"
    assert status_color("POLICY_DENIED") == "red"
    assert status_color("WAITING_APPROVAL") == "yellow"
    assert status_color("RUNNING") == "blue"
    assert status_color("CANCELLED") == "grey50"
    assert status_color("TIMEOUT") == "grey50"
    assert status_color("") == "white"
    assert status_color("NOT_A_STATE") == "white"


def test_risk_color_mapping() -> None:
    assert risk_color("HIGH") == "red"
    assert risk_color("MEDIUM") == "yellow"
    assert risk_color("LOW") == "grey50"
    assert risk_color("weird") == "white"


def test_flow_rows_projection_handles_unicode() -> None:
    rows = flow_rows({
        "items": [{
            "id": "f1", "goal": "Проверить артефакт sha256", "status": "WAITING_APPROVAL",
            "revision": 3, "created_at": "2026-07-26T10:02:00",
        }],
        "total": 1,
    })
    assert rows == [("f1", "Проверить артефакт sha256", "WAITING_APPROVAL", "3", "2026-07-26 10:02:00")]


def test_flow_rows_clips_long_goal() -> None:
    rows = flow_rows({"items": [{"id": "f1", "goal": "g" * 200}]})
    assert len(rows[0][1]) == 60


def test_approval_rows_projection() -> None:
    rows = approval_rows({
        "items": [{
            "id": "ap-01", "task_id": "b3c4b3c4b3c4", "tool_name": "sandbox.shell",
            "risk_level": "HIGH", "reason": "executes rm -rf on workspace tmp",
        }],
        "total": 1,
    })
    assert rows[0][0] == "ap-01"
    assert rows[0][1] == "b3c4b3c4"
    assert rows[0][3] == "HIGH"


def test_transition_row_renders_arrow() -> None:
    row = transition_row(1, {"from_state": None, "to_state": "RECEIVED", "actor": "gateway"})
    assert row[0] == "1"
    assert row[1] == "NONE -> RECEIVED"


def test_transition_rows_filter_by_target_state() -> None:
    assert len(transition_rows(SAMPLE_REPLAY)) == 3
    filtered = transition_rows(SAMPLE_REPLAY, "waiting")
    assert len(filtered) == 1
    assert filtered[0][1] == "PLANNING -> WAITING_APPROVAL"


def test_step_and_artifact_projections() -> None:
    labels = step_labels(SAMPLE_REPLAY)
    assert labels[0].startswith("#0 Execute verify [COMPLETED]")
    rows = artifact_rows(SAMPLE_REPLAY)
    assert rows == [("art-4d8c", "index.html", "42b", "yes")]


def test_flow_summary_text() -> None:
    text = flow_summary_text({"id": "b3c4", "goal": "цель", "status": "QUEUED", "revision": 2})
    assert "b3c4" in text and "цель" in text and "QUEUED" in text


# ── Widget behaviour ─────────────────────────────────────────────────


def test_refresh_populates_flow_and_approval_tables() -> None:
    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            assert app.query_one("#flows_table", DataTable).row_count == 1
            assert app.query_one("#approvals_table", DataTable).row_count == 1

    asyncio.run(_check())


def test_render_replay_builds_rows() -> None:
    """_render_replay fills transitions, the steps tree and the artifacts table."""

    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            await app._render_replay(SAMPLE_REPLAY)
            assert app.query_one("#replay_table", DataTable).row_count == 3
            assert app.query_one("#replay_artifacts_table", DataTable).row_count == 1
            tree = app.query_one("#replay_steps_tree", Tree)
            assert len(tree.root.children) == 2

    asyncio.run(_check())


def test_render_replay_honours_status_filter() -> None:
    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            app.query_one("#replay_status_filter", Input).value = "queued"
            await app._render_replay(SAMPLE_REPLAY)
            assert app.query_one("#replay_table", DataTable).row_count == 1

    asyncio.run(_check())


def test_load_replay_uses_selected_flow() -> None:
    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            app.selected_flow_id = "b3c4"
            await app.load_replay()
            assert app.query_one("#replay_table", DataTable).row_count == 3

    asyncio.run(_check())


@pytest.mark.skipif(sys.platform == "win32", reason='Textual TUI async timing differs on Windows (Wave 4)')
def test_approve_selected_calls_decide() -> None:
    """Pressing Approve routes through decide_approval(approve=True)."""

    async def _check() -> None:
        client = RecordingClient()
        app = AntigonaApp(client=client, refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            app.query_one("#approval_id_input", Input).value = "ap-01"
            await pilot.click("#btn_approve")
            await pilot.pause()
            assert client.decisions == [("ap-01", True)]
            assert app.query_one("#approvals_table", DataTable).row_count == 0

    asyncio.run(_check())


def test_reject_selected_calls_decide_with_false() -> None:
    async def _check() -> None:
        client = RecordingClient()
        app = AntigonaApp(client=client, refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            app.select_approval("ap-01")
            await pilot.click("#btn_reject")
            await pilot.pause()
            assert client.decisions == [("ap-01", False)]

    asyncio.run(_check())


def test_decide_without_selection_is_a_noop() -> None:
    async def _check() -> None:
        client = RecordingClient()
        app = AntigonaApp(client=client, refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            app.query_one("#approval_id_input", Input).value = ""
            app.selected_approval_id = None
            await app.decide_selected(True)
            assert client.decisions == []

    asyncio.run(_check())


def test_select_flow_switches_to_live_tab() -> None:
    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            app.select_flow("f1a2")
            await pilot.pause()
            assert app.selected_flow_id == "f1a2"
            assert app.query_one("#panes", ContentSwitcher).current == "pane-live"
            assert app.query_one("#replay_flow_input", Input).value == "f1a2"
            app.stop_stream()

    asyncio.run(_check())


def test_keyboard_bindings_switch_tabs() -> None:
    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            for key, pane in (("2", "pane-live"), ("3", "pane-approvals"), ("4", "pane-replay")):
                await pilot.press(key)
                await pilot.pause()
                assert app.query_one("#panes", ContentSwitcher).current == pane

    asyncio.run(_check())


def test_live_stream_messages_render_rows() -> None:
    async def _check() -> None:
        app = AntigonaApp(client=RecordingClient(), refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            app._handle_stream_message({
                "type": "transition", "from_state": "QUEUED", "to_state": "PLANNING",
                "actor": "worker", "reason": "started", "created_at": "2026-07-26T10:02:02",
            })
            app._handle_stream_message({"type": "end", "status": "CANCELLED"})
            app._handle_stream_message({"type": "error", "detail": "flow not found"})
            assert app.query_one("#live_table", DataTable).row_count == 1

    asyncio.run(_check())


def test_cancel_button_calls_gateway() -> None:
    async def _check() -> None:
        client = RecordingClient()
        app = AntigonaApp(client=client, refresh_interval=0)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            app.query_one("#flow_id_input", Input).value = "f1a2"
            await pilot.click("#btn_cancel")
            await pilot.pause()
            assert client.cancelled == ["f1a2"]

    asyncio.run(_check())
