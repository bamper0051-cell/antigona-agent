#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx

from antigona.config import Settings
from antigona.main import create_app


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(f"sqlite:///{root / 'e2e.db'}", root / "workspace", {"e2e-token": "e2e-owner"}))
        headers = {"Authorization": "Bearer e2e-token", "Idempotency-Key": "docker-e2e"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/tasks", headers=headers, json={"goal": "docker proof", "path": "proof.txt", "content": "sandboxed"})
                task_id = created.json()["id"]
                waiting = await client.post(f"/tasks/{task_id}/run", headers={"Authorization": "Bearer e2e-token"})
                approval_id = waiting.json()["approvals"][0]["id"]
                await client.post(f"/tasks/{task_id}/approvals/{approval_id}", headers={"Authorization": "Bearer e2e-token"}, json={"approve": True})
                completed = await client.post(f"/tasks/{task_id}/run", headers={"Authorization": "Bearer e2e-token"})
                body = completed.json()
                print(f"create={created.status_code} approval_gate={waiting.json()['status']} final={body['status']}")
                print(f"sandbox=docker verified={body['artifacts'][0]['verified']} sha256={body['artifacts'][0]['sha256']}")
                if body["status"] != "DONE" or not body["artifacts"][0]["verified"]:
                    raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
