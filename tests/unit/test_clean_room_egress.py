from __future__ import annotations

import re
from pathlib import Path


def test_clean_room_no_prohibited_identifiers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    target_files = [
        repo_root / "src" / "antigona" / "egress" / "proxy.py",
        repo_root / "src" / "antigona" / "egress" / "__init__.py",
        repo_root / "src" / "antigona" / "worker" / "tools" / "web_fetch_tool.py",
    ]

    prohibited_pattern = re.compile(r"\b(klio|hermes|openclaw)\b", re.IGNORECASE)

    for path in target_files:
        assert path.exists(), f"Target file missing: {path}"
        content = path.read_text(encoding="utf-8")
        matches = prohibited_pattern.findall(content)
        assert not matches, f"Prohibited identifier(s) {matches} found in {path}"


def test_web_fetch_tool_routes_solely_through_egress_proxy() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    target = repo_root / "src" / "antigona" / "worker" / "tools" / "web_fetch_tool.py"

    content = target.read_text(encoding="utf-8")
    forbidden_network_calls = ["urlopen", "requests.get", "httpx.get", "socket.create_connection"]

    for forbidden in forbidden_network_calls:
        assert forbidden not in content, (
            f"Direct network call '{forbidden}' found in web_fetch_tool.py. "
            "All network access must route through EgressProxy."
        )
