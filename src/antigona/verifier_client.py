from __future__ import annotations

import json
import urllib.request


class VerifierClient:
    def __init__(self, url: str, credential: str) -> None:
        self.url = url
        self.credential = credential

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        payload = json.dumps({"task_id": task_id, "correlation_id": correlation_id}).encode()
        request = urllib.request.Request(
            f"{self.url.rstrip('/')}/verify",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.credential}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            return str(json.load(response)["decision"])


