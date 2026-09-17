"""Ollama management tool — status / start serve / list / pull / switch provider.

Owner-requested: give Antigona the ability to run on a local Ollama LLM and to
manage ``ollama serve``. Compose-only with the single core (registered from
``register_builtins``); every action is fail-soft (returns a JSON status, never
raises through the tool boundary).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

OLLAMA_URL = "http://127.0.0.1:11434"


def _ollama_bin() -> str:
    return shutil.which("ollama") or "/usr/local/bin/ollama"


def _serving() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=3) as r:
            return bool(r.status == 200)
    except Exception:  # noqa: BLE001
        return False


def _list_models() -> list[str]:
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as r:
            data = _json.loads(r.read().decode("utf-8", "ignore"))
            return [str(m.get("name")) for m in data.get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def _start_serve() -> tuple[bool, str]:
    import time
    if _serving():
        return True, "ollama serve already running"
    subprocess.Popen(
        [_ollama_bin(), "serve"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(10):
        time.sleep(0.5)
        if _serving():
            return True, "ollama serve started"
    return False, "ollama serve did not become reachable in 5s"


async def _handle_ollama(*, action: str = "status", model: str = "", **kwargs: Any) -> str:
    act = (action or "status").strip().lower()
    if act == "status":
        return json.dumps(
            {"success": True, "serving": _serving(), "models": _list_models(), "binary": _ollama_bin()},
            ensure_ascii=False,
        )
    if act == "start":
        ok, msg = _start_serve()
        return json.dumps({"success": ok, "serving": _serving(), "message": msg, "models": _list_models()}, ensure_ascii=False)
    if act == "list":
        return json.dumps({"success": True, "models": _list_models()}, ensure_ascii=False)
    if act == "pull":
        if not model.strip():
            return json.dumps({"success": False, "error": "ollama pull requires a model name (e.g. qwen2.5:1.5b)"}, ensure_ascii=False)
        res = subprocess.run(
            [_ollama_bin(), "pull", model.strip()],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        return json.dumps(
            {"success": res.returncode == 0, "model": model.strip(), "output": (res.stdout or res.stderr)[-400:]},
            ensure_ascii=False,
        )
    if act == "switch":
        from antigona.tools.provider_switcher import switch_to_provider
        ok, msg = switch_to_provider("ollama")
        return json.dumps({"success": ok, "message": msg}, ensure_ascii=False)
    return json.dumps({"success": False, "error": f"unknown ollama action '{act}' (status|start|list|pull|switch)"}, ensure_ascii=False)


_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "status | start (ollama serve) | list | pull | switch (to local Ollama)",
        },
        "model": {"type": "string", "description": "Model name for pull (e.g. qwen2.5:1.5b)"},
    },
    "required": [],
}


def register(registry: Any) -> None:
    registry.register("ollama", toolset="llm", schema=_SCHEMA, handler=_handle_ollama, replace=True)
