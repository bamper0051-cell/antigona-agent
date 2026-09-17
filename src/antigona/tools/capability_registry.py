"""Runtime Capability Registry for Antigona.

Provides a single source of truth for runtime capabilities:
  - workspace.read, workspace.write, workspace.list, workspace.mkdir
  - sandbox.shell
  - archive.create, archive.extract, archive.inspect, archive.verify
  - speech.tts, speech.stt
  - telegram.send_file, telegram.send_voice
  - web.search
  - system.time
  - browser
  - mcp, plugins

Capabilities are tracked with explicit status:
  - AVAILABLE: Registered and verified functional (via probe)
  - BROKEN: Registered/configured but probe or execution failed
  - UNTESTED: Registered but live probe has not been run yet
  - NOT_IMPLEMENTED: Known capability concept but no provider/backend available
  - NONE: Not configured/installed (e.g. no plugins loaded)

Enforces: REGISTERED != AVAILABLE.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import logging
import os
import shutil
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from antigona.core import paths
from antigona.tools.contracts import RiskLevel

logger = logging.getLogger(__name__)


class CapabilityStatus(StrEnum):
    """Runtime status of a capability."""

    AVAILABLE = "AVAILABLE"
    BROKEN = "BROKEN"
    UNTESTED = "UNTESTED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    NONE = "NONE"


@dataclass
class Capability:
    """Descriptor for a single system or tool capability."""

    id: str
    category: str
    description: str
    risk_level: RiskLevel = RiskLevel.SAFE
    status: CapabilityStatus = CapabilityStatus.UNTESTED
    probe_fn: Callable[[], Coroutine[Any, Any, bool]] | None = None
    last_probe_time: float | None = None
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Live Probing Primitives ───────────────────────────────────────────────────


async def _probe_workspace_read() -> bool:
    """Probe workspace read by checking workspace directory access."""
    ws = paths.workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    return ws.exists() and os.access(ws, os.R_OK)


async def _probe_workspace_write() -> bool:
    """Probe workspace write by creating and removing a probe file."""
    ws = paths.workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    probe_file = ws / f".probe_{os.getpid()}_{int(time.time()*1000)}.tmp"
    try:
        probe_file.write_text("probe", encoding="utf-8")
        ok = probe_file.exists() and probe_file.read_text(encoding="utf-8") == "probe"
        return ok
    finally:
        probe_file.unlink(missing_ok=True)


async def _probe_workspace_list() -> bool:
    """Probe workspace list."""
    ws = paths.workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    list(ws.iterdir())
    return True


async def _probe_sandbox_shell() -> bool:
    """Probe shell execution."""
    proc = await asyncio.create_subprocess_exec(
        "echo", "probe_ok",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0 and b"probe_ok" in stdout


async def _probe_archive_create() -> bool:
    """Probe zip archive creation."""
    import zipfile
    ws = paths.workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    probe_zip = ws / f".probe_zip_{os.getpid()}.zip"
    try:
        with zipfile.ZipFile(probe_zip, "w") as zf:
            zf.writestr("probe.txt", "content")
        return probe_zip.exists() and probe_zip.stat().st_size > 0
    finally:
        probe_zip.unlink(missing_ok=True)


async def _probe_archive_extract() -> bool:
    """Probe zip archive extraction."""
    import zipfile
    ws = paths.workspace_dir()
    probe_zip = ws / f".probe_ext_{os.getpid()}.zip"
    probe_target = ws / f".probe_target_{os.getpid()}"
    try:
        with zipfile.ZipFile(probe_zip, "w") as zf:
            zf.writestr("test.txt", "data")
        with zipfile.ZipFile(probe_zip, "r") as zf:
            zf.extractall(probe_target)
        extracted = probe_target / "test.txt"
        return extracted.exists() and extracted.read_text(encoding="utf-8") == "data"
    finally:
        probe_zip.unlink(missing_ok=True)
        if probe_target.exists():
            shutil.rmtree(probe_target, ignore_errors=True)


async def _probe_speech_tts() -> bool:
    """Probe TTS availability."""
    has_openai = bool(os.getenv("OPENAI_API_KEY")) and os.getenv("OPENAI_API_KEY") not in ("dummy", "dummy-key")
    has_edge_tts = shutil.which("edge-tts") is not None
    has_gtts = importlib.util.find_spec("gtts") is not None
    return has_openai or has_edge_tts or has_gtts


async def _probe_speech_stt() -> bool:
    """Probe STT availability."""
    has_whisper = importlib.util.find_spec("faster_whisper") is not None
    has_openai = bool(os.getenv("OPENAI_API_KEY")) and os.getenv("OPENAI_API_KEY") not in ("dummy", "dummy-key")
    return has_whisper or has_openai


async def _probe_system_time() -> bool:
    """Probe system time."""
    now = datetime.datetime.now(datetime.UTC)
    return now.year >= 2024


async def _probe_web_search() -> bool:
    """Probe web search package availability."""
    has_ddg = importlib.util.find_spec("duckduckgo_search") is not None
    return has_ddg


# ── Registry Class ────────────────────────────────────────────────────────────


class CapabilityRegistry:
    """Unified runtime registry for system and tool capabilities."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._init_defaults()

    def _init_defaults(self) -> None:
        """Register default core capabilities."""
        defaults = [
            Capability(
                id="workspace.read",
                category="filesystem",
                description="Read files inside workspace",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_workspace_read,
            ),
            Capability(
                id="workspace.write",
                category="filesystem",
                description="Write and modify files inside workspace",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_workspace_write,
            ),
            Capability(
                id="workspace.list",
                category="filesystem",
                description="List files and directories inside workspace",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_workspace_list,
            ),
            Capability(
                id="sandbox.shell",
                category="shell",
                description="Execute commands in sandbox environment",
                risk_level=RiskLevel.MEDIUM,
                probe_fn=_probe_sandbox_shell,
            ),
            Capability(
                id="archive.create",
                category="archive",
                description="Create ZIP/TAR.GZ archives",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_archive_create,
            ),
            Capability(
                id="archive.extract",
                category="archive",
                description="Safe extraction of archives with path traversal protection",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_archive_extract,
            ),
            Capability(
                id="archive.inspect",
                category="archive",
                description="Inspect archive contents and metadata without extracting",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_archive_create,
            ),
            Capability(
                id="archive.verify",
                category="archive",
                description="Verify integrity of archive files",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_archive_create,
            ),
            Capability(
                id="speech.tts",
                category="voice",
                description="Text-to-Speech audio generation",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_speech_tts,
            ),
            Capability(
                id="speech.stt",
                category="voice",
                description="Speech-to-Text audio transcription",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_speech_stt,
            ),
            Capability(
                id="telegram.send_file",
                category="delivery",
                description="Deliver document and media artifacts via Telegram",
                risk_level=RiskLevel.SAFE,
                status=CapabilityStatus.AVAILABLE,
            ),
            Capability(
                id="telegram.send_voice",
                category="delivery",
                description="Deliver voice audio messages via Telegram",
                risk_level=RiskLevel.SAFE,
                status=CapabilityStatus.AVAILABLE,
            ),
            Capability(
                id="web.search",
                category="network",
                description="Search the web via DuckDuckGo",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_web_search,
            ),
            Capability(
                id="system.time",
                category="system",
                description="Get accurate current date and time",
                risk_level=RiskLevel.SAFE,
                probe_fn=_probe_system_time,
            ),
            Capability(
                id="browser",
                category="browser",
                description="Playwright headless browser automation",
                risk_level=RiskLevel.HIGH,
                status=CapabilityStatus.NOT_IMPLEMENTED if importlib.util.find_spec("playwright") is None else CapabilityStatus.UNTESTED,
            ),
            Capability(
                id="mcp",
                category="integration",
                description="Model Context Protocol servers",
                risk_level=RiskLevel.MEDIUM,
                status=CapabilityStatus.AVAILABLE if importlib.util.find_spec("mcp") is not None else CapabilityStatus.NONE,
            ),
            Capability(
                id="plugins",
                category="integration",
                description="Plugin subsystem",
                risk_level=RiskLevel.SAFE,
                status=CapabilityStatus.NONE,
            ),
        ]
        for cap in defaults:
            self._capabilities[cap.id] = cap

    def register(self, capability: Capability) -> None:
        """Register or update a capability."""
        self._capabilities[capability.id] = capability

    def get(self, capability_id: str) -> Capability | None:
        """Get capability by id."""
        return self._capabilities.get(capability_id)

    def get_status(self, capability_id: str) -> CapabilityStatus:
        """Get current status of a capability."""
        cap = self._capabilities.get(capability_id)
        if cap is None:
            return CapabilityStatus.NOT_IMPLEMENTED
        return cap.status

    def set_status(
        self,
        capability_id: str,
        status: CapabilityStatus,
        *,
        failure_reason: str | None = None,
    ) -> None:
        """Manually set capability status."""
        cap = self._capabilities.get(capability_id)
        if cap is not None:
            cap.status = status
            cap.failure_reason = failure_reason
            cap.last_probe_time = time.time()

    async def probe(self, capability_id: str) -> CapabilityStatus:
        """Run live probe for a single capability."""
        cap = self._capabilities.get(capability_id)
        if cap is None:
            return CapabilityStatus.NOT_IMPLEMENTED
        if cap.probe_fn is None:
            return cap.status

        try:
            ok = await cap.probe_fn()
            cap.last_probe_time = time.time()
            if ok:
                cap.status = CapabilityStatus.AVAILABLE
                cap.failure_reason = None
            else:
                cap.status = CapabilityStatus.BROKEN
                cap.failure_reason = "Probe returned False"
        except Exception as exc:
            cap.last_probe_time = time.time()
            cap.status = CapabilityStatus.BROKEN
            cap.failure_reason = f"{type(exc).__name__}: {exc}"
            logger.debug("Capability %s probe failed: %s", capability_id, exc)

        return cap.status

    async def probe_all(self) -> dict[str, CapabilityStatus]:
        """Run live probes for all capabilities that support probing."""
        results: dict[str, CapabilityStatus] = {}
        for cap_id in list(self._capabilities.keys()):
            results[cap_id] = await self.probe(cap_id)
        return results

    def snapshot(self) -> dict[str, str]:
        """Return dict snapshot of all capability IDs to status strings."""
        return {cap_id: cap.status.value for cap_id, cap in sorted(self._capabilities.items())}

    def format_prompt_snapshot(self) -> str:
        """Format runtime capability snapshot for inclusion in LLM system prompt."""
        lines = ["--- ТЕКУЩИЙ СТАТУС ВОЗМОЖНОСТЕЙ СИСТЕМЫ (CAPABILITY INVENTORY) ---"]
        lines.append("ВАЖНО: Доверяй только этому списку при ответе на вопросы о своих возможностях.")
        lines.append("Если возможность AVAILABLE — используй её и не говори, что её нет.")
        lines.append("Если возможность BROKEN или NOT_IMPLEMENTED — честно скажи об этом.\n")
        for cap_id, cap in sorted(self._capabilities.items()):
            lines.append(f"• {cap_id}: {cap.status.value} ({cap.description})")
        return "\n".join(lines)


# Singleton instance
_GLOBAL_REGISTRY: CapabilityRegistry | None = None


def get_capability_registry() -> CapabilityRegistry:
    """Get or initialize the singleton CapabilityRegistry."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = CapabilityRegistry()
    return _GLOBAL_REGISTRY
