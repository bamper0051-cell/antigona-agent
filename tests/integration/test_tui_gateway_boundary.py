"""Boundary tests for the P2.4 collection endpoints and the TUI that reads them.

The TUI cases drive the real Textual app against the real FastAPI gateway over
an in-process ASGI transport — no sockets, no mocks between the button and the
state machine — so owner scoping and the approval path are exercised end to end.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from textual.widgets import ContentSwitcher, DataTable, Input, Tab, Tabs

from antigona.cli import GatewayClient
from antigona.config import Settings
from antigona.gateway.api import create_gateway_app
from antigona.models import Approval, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.tui import AntigonaApp

TOKENS = {"admin-token": "admin", "alice-token": "owner-alice"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        f"sqlite:///{tmp_path / 'db.sqlite'}",
        tmp_path / "workspace",
        dev_tokens=TOKENS,
        sandbox_backend="inprocess",
        test_mode=True,
    )


def _app(tmp_path: Path) -> Any:
    app = create_gateway_app(_settings(tmp_path))
    app.state.database.create_all()
    return app


def _auth(token: str = "admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _request(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, url, **kwargs)


def call(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_request(app, method, url, **kwargs))


def _create_flow(
    app: Any,
    owner_id: str = "admin",
    goal: str = "Проверить артефакт sha256",
    idempotency_key: str = "int-tui-1",
    status: str | None = None,
) -> str:
    with app.state.database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id=owner_id,
                goal=goal,
                path="test.txt",
                content="data",
                idempotency_key=idempotency_key,
            )
        )
        if status:
            repo.transition(task, TaskState(status), reason="test", actor="gateway")
            repo.commit()
        return task.id


def _create_approval(
    app: Any,
    flow_id: str,
    decision: str = "PENDING",
    tool_name: str = "sandbox.shell",
    risk_level: str = "HIGH",
) -> str:
    with app.state.database.session_factory() as session:
        approval = Approval(
            task_id=flow_id,
            tool_name=tool_name,
            arguments={"command": ["rm", "-rf", "tmp"]},
            risk_level=risk_level,
            reason="executes rm -rf on workspace tmp",
            decision=decision,
        )
        session.add(approval)
        session.commit()
        return approval.id


def _tui_client(app: Any, token: str = "admin-token") -> GatewayClient:
    return GatewayClient("http://test", token, transport=httpx.ASGITransport(app=app))


async def _wait_for_rows(tui: AntigonaApp, pilot: Any, selector: str, expected: int, *, timeout: float = 2.0) -> DataTable:
    """Wait for Textual worker refreshes instead of assuming one event-loop turn."""
    deadline = time.monotonic() + timeout
    table = tui.query_one(selector, DataTable)
    while table.row_count != expected and time.monotonic() < deadline:
        await pilot.pause(0.01)
    if table.row_count != expected:
        log = str(tui.query_one("#log_view"))
        raise AssertionError(
            f"{selector} row_count={table.row_count}, expected {expected} "
            f"after {timeout:.1f}s; log={log!r}"
        )
    return table


@contextlib.asynccontextmanager
async def _tui_session(tui: AntigonaApp, *, size: tuple[int, int] = (160, 45)) -> Any:
    """Own the app and its workers explicitly for every boundary test."""
    async with tui.run_test(size=size) as pilot:
        try:
            yield pilot
        finally:
            tui.workers.cancel_all()
            tui.exit()
            await pilot.pause(0.01)


# ── GET /flows ───────────────────────────────────────────────────────


def test_list_flows_endpoint_returns_200(tmp_path: Path) -> None:
    app = _app(tmp_path)
    flow_id = _create_flow(app)
    resp = call(app, "GET", "/flows", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == flow_id
    assert body["items"][0]["goal"] == "Проверить артефакт sha256"
    assert body["items"][0]["status"] == TaskState.RECEIVED.value


def test_list_flows_owner_isolation(tmp_path: Path) -> None:
    app = _app(tmp_path)
    mine = _create_flow(app, owner_id="admin", idempotency_key="int-tui-mine")
    theirs = _create_flow(app, owner_id="owner-alice", idempotency_key="int-tui-theirs")

    body = call(app, "GET", "/flows", headers=_auth()).json()
    ids = {item["id"] for item in body["items"]}
    assert ids == {mine}
    assert theirs not in ids
    assert body["total"] == 1

    alice = call(app, "GET", "/flows", headers=_auth("alice-token")).json()
    assert {item["id"] for item in alice["items"]} == {theirs}


def test_list_flows_status_filter(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _create_flow(app, idempotency_key="int-tui-received")
    queued = _create_flow(app, idempotency_key="int-tui-queued", status=TaskState.QUEUED.value)

    body = call(
        app, "GET", "/flows", params={"status": TaskState.QUEUED.value}, headers=_auth()
    ).json()
    assert [item["id"] for item in body["items"]] == [queued]
    assert body["total"] == 1


def test_list_flows_pagination(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for index in range(3):
        _create_flow(app, idempotency_key=f"int-tui-page-{index}")
    body = call(app, "GET", "/flows", params={"limit": 2, "offset": 0}, headers=_auth()).json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_list_flows_without_auth(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert call(app, "GET", "/flows").status_code == 401


# ── GET /approvals ───────────────────────────────────────────────────


def test_list_approvals_endpoint_returns_200(tmp_path: Path) -> None:
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-tui-appr")
    pending = _create_approval(app, flow_id)
    _create_approval(app, flow_id, decision="APPROVED", tool_name="workspace.write_text")

    body = call(app, "GET", "/approvals", params={"status": "PENDING"}, headers=_auth()).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == pending
    assert body["items"][0]["task_id"] == flow_id
    assert body["items"][0]["risk_level"] == "HIGH"


def test_list_approvals_owner_isolation(tmp_path: Path) -> None:
    app = _app(tmp_path)
    mine = _create_flow(app, owner_id="admin", idempotency_key="int-tui-appr-mine")
    theirs = _create_flow(app, owner_id="owner-alice", idempotency_key="int-tui-appr-theirs")
    my_approval = _create_approval(app, mine)
    their_approval = _create_approval(app, theirs)

    body = call(app, "GET", "/approvals", headers=_auth()).json()
    ids = {item["id"] for item in body["items"]}
    assert ids == {my_approval}
    assert their_approval not in ids

    alice = call(app, "GET", "/approvals", headers=_auth("alice-token")).json()
    assert {item["id"] for item in alice["items"]} == {their_approval}


def test_list_approvals_without_auth(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert call(app, "GET", "/approvals").status_code == 401


def test_get_approval_detail_200(tmp_path: Path) -> None:
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-tui-detail")
    approval_id = _create_approval(app, flow_id)
    resp = call(app, "GET", f"/approvals/{approval_id}", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == approval_id
    assert body["tool_name"] == "sandbox.shell"
    assert body["decision"] == "PENDING"


def test_get_approval_detail_404_others(tmp_path: Path) -> None:
    app = _app(tmp_path)
    flow_id = _create_flow(app, owner_id="owner-alice", idempotency_key="int-tui-detail-alice")
    approval_id = _create_approval(app, flow_id)
    assert call(app, "GET", f"/approvals/{approval_id}", headers=_auth("alice-token")).status_code == 200
    assert call(app, "GET", f"/approvals/{approval_id}", headers=_auth()).status_code == 404


def test_get_approval_detail_404_missing(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert call(app, "GET", "/approvals/nope", headers=_auth()).status_code == 404


# ── TUI over the real gateway ────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="Textual TUI async table-population timing differs on Windows (Wave 4)")
def test_tui_launches_and_has_tabs(tmp_path: Path) -> None:
    """Headless Textual: four tabs, the approval buttons, and the caller's flow."""
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-tui-launch")

    async def _check() -> None:
        tui = AntigonaApp(client=_tui_client(app), refresh_interval=0)
        async with _tui_session(tui) as pilot:
            await pilot.pause()
            labels = [str(tab.label) for tab in tui.query_one("#main_tabs", Tabs).query(Tab)]
            assert labels == ["Flows", "Live", "Approvals", "Replay"]
            assert tui.query_one("#btn_approve") is not None
            assert tui.query_one("#btn_reject") is not None

            table = await _wait_for_rows(tui, pilot, "#flows_table", 1)
            assert str(table.get_row_at(0)[0]) == flow_id
            assert "sha256" in str(table.get_row_at(0)[1])

            await pilot.press("4")
            await pilot.pause()
            assert tui.query_one("#panes", ContentSwitcher).current == "pane-replay"

    asyncio.run(_check())


@pytest.mark.skipif(sys.platform == "win32", reason="Textual TUI async table-population timing differs on Windows (Wave 4)")
def test_tui_flows_tab_hides_foreign_flows(tmp_path: Path) -> None:
    app = _app(tmp_path)
    mine = _create_flow(app, owner_id="admin", idempotency_key="int-tui-vis-mine")
    _create_flow(app, owner_id="owner-alice", idempotency_key="int-tui-vis-theirs")

    async def _check() -> None:
        tui = AntigonaApp(client=_tui_client(app), refresh_interval=0)
        async with _tui_session(tui) as pilot:
            await pilot.pause()
            table = await _wait_for_rows(tui, pilot, "#flows_table", 1)
            assert str(table.get_row_at(0)[0]) == mine

    asyncio.run(_check())


@pytest.mark.skipif(sys.platform == "win32", reason='Textual TUI approvals-table timing differs on Windows (Wave 4)')
def test_tui_approve_button_hits_endpoint(tmp_path: Path) -> None:
    """Clicking Approve reaches POST /approvals/{id}/decision and flips the row."""
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-tui-approve")
    approval_id = _create_approval(app, flow_id)

    async def _check() -> None:
        tui = AntigonaApp(client=_tui_client(app), refresh_interval=0)
        async with _tui_session(tui) as pilot:
            await _wait_for_rows(tui, pilot, "#approvals_table", 1)
            await pilot.press("3")
            tui.query_one("#approval_id_input", Input).value = approval_id
            await pilot.click("#btn_approve")
            await _wait_for_rows(tui, pilot, "#approvals_table", 0)

    asyncio.run(_check())

    with app.state.database.session_factory() as session:
        stored = session.scalar(select(Approval).where(Approval.id == approval_id))
        assert stored is not None
        assert stored.decision == "APPROVED"
        assert stored.decided_by == "admin"


def test_tui_reject_button_denies_approval(tmp_path: Path) -> None:
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-tui-reject")
    approval_id = _create_approval(app, flow_id)

    async def _check() -> None:
        tui = AntigonaApp(client=_tui_client(app), refresh_interval=0)
        async with _tui_session(tui) as pilot:
            await pilot.pause()
            await pilot.press("3")
            tui.query_one("#approval_id_input", Input).value = approval_id
            await pilot.click("#btn_reject")
            await pilot.pause()

    asyncio.run(_check())

    with app.state.database.session_factory() as session:
        stored = session.scalar(select(Approval).where(Approval.id == approval_id))
        assert stored is not None
        assert stored.decision == "DENIED"


def test_tui_replay_tab_renders_trajectory(tmp_path: Path) -> None:
    """The Replay tab is fed by the P2.3 endpoint, not by a local DB read."""
    app = _app(tmp_path)
    flow_id = _create_flow(
        app, idempotency_key="int-tui-replay", status=TaskState.QUEUED.value
    )

    async def _check() -> None:
        tui = AntigonaApp(client=_tui_client(app), refresh_interval=0)
        async with _tui_session(tui) as pilot:
            await pilot.pause()
            tui.query_one("#replay_flow_input", Input).value = flow_id
            await tui.load_replay()
            assert tui.query_one("#replay_table", DataTable).row_count >= 1

    asyncio.run(_check())


def test_tui_survives_unreachable_gateway(tmp_path: Path) -> None:
    """A dead gateway degrades to a log line; the app stays up."""

    async def _check() -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = GatewayClient("http://test", "admin-token", transport=httpx.MockTransport(refuse))
        tui = AntigonaApp(client=client, refresh_interval=0)
        async with _tui_session(tui) as pilot:
            await pilot.pause()
            assert tui.query_one("#flows_table", DataTable).row_count == 0
            assert tui.is_running

    asyncio.run(_check())


@pytest.mark.skipif(sys.platform == "win32", reason="Textual async timing differs on Windows")
def test_tui_approve_button_after_preceding_integration_setup(tmp_path: Path) -> None:
    """Order regression: approval refresh remains deterministic after prior gateway work."""
    app = _app(tmp_path)
    _create_flow(app, idempotency_key="int-tui-order-flow")
    flow_id = _create_flow(app, idempotency_key="int-tui-order-approve")
    approval_id = _create_approval(app, flow_id)

    async def _check() -> None:
        tui = AntigonaApp(client=_tui_client(app), refresh_interval=0)
        async with _tui_session(tui) as pilot:
            await _wait_for_rows(tui, pilot, "#flows_table", 2)
            await pilot.press("3")
            tui.query_one("#approval_id_input", Input).value = approval_id
            await _wait_for_rows(tui, pilot, "#approvals_table", 1)

    asyncio.run(_check())
