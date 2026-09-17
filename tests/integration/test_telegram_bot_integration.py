from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from antigona.channels.telegram.bot import TelegramBot
from antigona.cli import GatewayClient
from antigona.config import Settings
from antigona.gateway.api import create_gateway_app


def test_telegram_bot_flow_creation_and_approval_mock(
    tmp_path: Path, monkeypatch: Any
) -> None:
    db_path = tmp_path / "bot_test.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        workspace=workspace,
        dev_tokens={"gateway-token": "owner-1"},
        sandbox_backend="inprocess",
        test_mode=True,
    )
    app = create_gateway_app(settings)

    with TestClient(app) as client:
        bot = TelegramBot(
            token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            gateway_url="http://testserver",
            gateway_token="gateway-token",
        )
        cli_client = GatewayClient("http://testserver", "gateway-token")

        async def fake_post(
            self_client: Any,
            url: str,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            **kwargs: Any,
        ) -> Any:
            endpoint = url.replace("http://testserver", "")
            merged_headers = {**dict(self_client.headers), **(headers or {})}
            return client.post(endpoint, json=json, headers=merged_headers)

        async def fake_get(
            self_client: Any,
            url: str,
            headers: dict[str, str] | None = None,
            **kwargs: Any,
        ) -> Any:
            endpoint = url.replace("http://testserver", "")
            merged_headers = {**dict(self_client.headers), **(headers or {})}
            return client.get(endpoint, headers=merged_headers)

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        monkeypatch.setattr("httpx.AsyncClient.get", fake_get)

        async def _test() -> None:
            try:
                # 1. Bot creates flow via Gateway POST /flows
                flow = await bot.post_flow(
                    goal="Create proof file",
                    path="proof.txt",
                    content="bot proof content",
                )
                assert flow is not None
                assert flow["goal"] == "Create proof file"
                flow_id = flow["id"]

                # 2. Bot queries flow via Gateway GET /flows/{id}
                flow_details = await bot.get_flow(flow_id)
                assert flow_details["id"] == flow_id
                assert flow_details["status"] in {"CREATED", "QUEUED"}

                # 3. Test CLI client get_flow and cancel_flow
                cli_flow = await cli_client.get_flow(flow_id)
                assert cli_flow.flow_id == flow_id

                cancel_res = await cli_client.cancel_flow(flow_id)
                assert cancel_res["status"] == "CANCELLED"
            finally:
                await cli_client.close()
                await bot.close()
                await bot.gateway_client.close()
                bot.database.dispose()

        asyncio.run(_test())
        app.state.database.dispose()
