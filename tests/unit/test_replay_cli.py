"""CLI-level tests for `antigona replay` (P2.3.e / P2.3.l).

These pin the *invocation form* documented in the P2.3 plan §2.5 and in
ADR-0007: `antigona replay <flow_id> [--json|--timeline] [--actor ...]`, with
options written **after** the positional argument.

That form is not a given. Wiring `replay` as a Typer sub-app (a Click *group*
with `invoke_without_command=True`, as `cron` and `skills` legitimately are)
makes Click read every token after the positional as a subcommand name, and the
documented call fails with "Missing argument 'flow_id'" while the undocumented
`replay --token X <flow_id>` still works. Replay has no subcommands, so it is a
plain command — and these tests fail if anyone converts it back.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from antigona import cli

runner = CliRunner()

FLOW_ID = "flow-abc-123"


@pytest.fixture(autouse=True)
def _gateway_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic: CLI resolves the gateway token from --token → env → .env.
    On CI there is no .env, so pin a token so the command reaches the mocked
    GatewayClient instead of failing with 'Gateway token is required'."""
    monkeypatch.setenv("ANTIGONA_GATEWAY_TOKEN", "test-token")

TRAJECTORY: dict[str, Any] = {
    "task_id": FLOW_ID,
    "owner_id": "owner-a",
    "goal": "Сгенерировать еженедельный health-report",
    "target_path": "reports/weekly.txt",
    "status": "DONE",
    "revision": 7,
    "created_at": "2026-07-26T10:00:00",
    "updated_at": "2026-07-26T10:05:23",
    "steps": [
        {
            "id": "step-001",
            "index": 0,
            "title": "Execute workspace.write_text and verify",
            "status": "COMPLETED",
            "input": {"path": "reports/weekly.txt"},
            "output": {"exit_code": 0},
            "retries": 0,
        }
    ],
    "transitions": [
        {
            "id": 1,
            "entity_id": FLOW_ID,
            "entity_type": "task",
            "from_state": None,
            "to_state": "RECEIVED",
            "reason": "gateway accepted task",
            "actor": "gateway",
            "created_at": "2026-07-26T10:00:00",
        }
    ],
    "artifacts": [],
}

TIMELINE: dict[str, Any] = {
    "task_id": FLOW_ID,
    "entries": [
        {
            "type": "transition",
            "timestamp": "2026-07-26T10:00:00",
            "entity_id": FLOW_ID,
            "description": "NONE -> RECEIVED  gateway (task)",
            "details": {"to_state": "RECEIVED"},
        }
    ],
}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    """Record what the CLI asks the Gateway for, without any network."""
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_replay(self: Any, *args: Any) -> dict[str, Any]:
        recorded.append(("replay", args))
        return TRAJECTORY

    async def fake_timeline(self: Any, *args: Any) -> dict[str, Any]:
        recorded.append(("timeline", args))
        return TIMELINE

    monkeypatch.setattr(cli.GatewayClient, "get_replay", fake_replay)
    monkeypatch.setattr(cli.GatewayClient, "get_replay_timeline", fake_timeline)
    return recorded


def test_replay_accepts_options_after_flow_id(calls: list[tuple[str, tuple[Any, ...]]]) -> None:
    result = runner.invoke(cli.app, ["replay", FLOW_ID, "--token", "tok-a"])
    assert result.exit_code == 0, result.output
    assert calls == [("replay", (FLOW_ID, None, None, None, None))]
    assert "DONE" in result.output


def test_replay_json_is_parseable_and_preserves_cyrillic(
    calls: list[tuple[str, tuple[Any, ...]]],
) -> None:
    result = runner.invoke(cli.app, ["replay", FLOW_ID, "--json"])
    assert result.exit_code == 0, result.output
    # Must survive `antigona replay <id> --json > replay.json`.
    payload = json.loads(result.output)
    assert payload["task_id"] == FLOW_ID
    assert payload["goal"] == "Сгенерировать еженедельный health-report"


def test_replay_timeline_uses_timeline_endpoint(
    calls: list[tuple[str, tuple[Any, ...]]],
) -> None:
    result = runner.invoke(cli.app, ["replay", FLOW_ID, "--timeline"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "timeline"
    assert f"Timeline for {FLOW_ID}" in result.output


def test_replay_forwards_filters(calls: list[tuple[str, tuple[Any, ...]]]) -> None:
    result = runner.invoke(
        cli.app,
        [
            "replay", FLOW_ID,
            "--actor", "verifier",
            "--entity-type", "step",
            "--from", "2026-07-01T00:00:00",
            "--to", "2026-07-31T00:00:00",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ("replay", (FLOW_ID, "verifier", "step", "2026-07-01T00:00:00", "2026-07-31T00:00:00"))
    ]


def test_replay_reports_missing_flow_as_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    async def not_found(self: Any, *args: Any) -> dict[str, Any]:
        request = httpx.Request("GET", "http://gw/flows/x/replay")
        raise httpx.HTTPStatusError(
            "404", request=request, response=httpx.Response(404, request=request)
        )

    monkeypatch.setattr(cli.GatewayClient, "get_replay", not_found)
    result = runner.invoke(cli.app, ["replay", "nope-123"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_replay_params_drops_unset_filters() -> None:
    """Only set replay filters are forwarded; unset ones are dropped.

    ``get_replay`` (the current replay-params API) builds the query dict from
    the provided ``actor``/``entity_type``/``from_dt``/``to_dt`` and omits any
    that are unset — pinned here through the real HTTP client path.
    """
    import asyncio

    import httpx

    def _client(seen: list[httpx.Request]) -> cli.GatewayClient:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        return cli.GatewayClient(
            "http://gw.test", "tok", transport=httpx.MockTransport(handler)
        )

    # No filters set → no query params at all
    seen: list[httpx.Request] = []
    asyncio.run(_client(seen).get_replay("flow-1"))
    assert dict(seen[0].url.params) == {}

    # Only actor set → only actor in params
    seen.clear()
    asyncio.run(_client(seen).get_replay("flow-1", actor="verifier"))
    assert dict(seen[0].url.params) == {"actor": "verifier"}

    # All filters set → all forwarded
    seen.clear()
    asyncio.run(
        _client(seen).get_replay(
            "flow-1",
            actor="worker",
            entity_type="step",
            from_dt="2026-07-01",
            to_dt="2026-07-31",
        )
    )
    assert dict(seen[0].url.params) == {
        "actor": "worker",
        "entity_type": "step",
        "from_dt": "2026-07-01",
        "to_dt": "2026-07-31",
    }
