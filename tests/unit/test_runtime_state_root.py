"""Regression: the read-only-code-root write class (Phase C.2).

The durable Telegram turn ledger defaulted to ``<code_root>/.tasks/telegram_turns.db``
and every other runtime-writable path followed the same pattern, so a hardened
deployment (``ProtectSystem=strict``) hit ``OSError: [Errno 30]`` and the bot
degraded to "continuing without recovery". This locks the class-level fix in:

* every runtime-writable path resolves through the single governed resolver
  (``ANTIGONA_STATE_ROOT`` / dedicated overrides) and lands OUTSIDE the code
  root in immutable mode for gateway/bot/worker/orchestration/delivery/verifier;
* an immutable deployment without a state root FAILS CLOSED with an actionable
  error instead of silently writing under the read-only code root;
* intentional dev/test defaults are preserved;
* the exactly-once turn ledger survives a process restart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from antigona.channels.telegram.turn_ledger import (
    ClaimOutcome,
    TurnLedger,
    ensure_ledger_writable,
)
from antigona.core import paths

#: Every path a running service may create, lock or write.
RUNTIME_RESOLVERS: dict[str, object] = {
    "tasks_dir": paths.tasks_dir,
    "turn_ledger": paths.turn_ledger_path,
    "reply_map": paths.reply_map_file,
    "context_chat": lambda: paths.context_chat_file(1),
    "voice_cache": paths.voice_cache_dir,
    "voice_settings": paths.voice_settings_file,
    "memory_db": paths.memory_db_path,
    "downloads": paths.downloads_dir,
    "workspace": paths.workspace_dir,
    "security": paths.security_dir,
    "health": paths.health_dir,
    "channel_log": paths.channel_log_file,
    "provider_state": paths.provider_state_file,
    "legacy_reachability": paths.legacy_reachability_log,
    "evidence_dir": paths.evidence_dir,
    "database": paths.database_path,
    "sessions_db": paths.sessions_db_path,
    # Owner-class runtime state (audit DB / elevation / traces / CLI state /
    # MCP registry / ownership ledgers / CLI aliases+themes).  Same write class:
    # these used to default under owner_dir(), which IS the read-only code root
    # in a hardened install where the service runs as root.
    "traces_db": paths.traces_db,
    "traces_log": paths.traces_log,
    "cli_state": paths.cli_state_file,
    "mcp_registry": paths.mcp_registry_file,
    "audit_log_db": paths.audit_log_db,
    "elevation_db": paths.elevation_db,
    "owner_pin_file": paths.owner_pin_file,
    "ownership_db_dir": paths.ownership_db_dir,
    "cli_aliases": paths.cli_aliases_file,
    "cli_theme": paths.cli_theme_file,
    "cli_user_themes": paths.cli_user_themes_file,
}

#: Read-only deployment artifacts legitimately resolved from the code root.
IMMUTABLE_ARTIFACTS: dict[str, object] = {
    "soul": paths.soul_file,
    "agents": paths.agents_file,
    "personalities": paths.personalities_dir,
    "gateway_config": paths.gateway_config_path,
    "project_local_dir": paths.project_local_dir,
}

#: Per-service resolver surface (gateway/bot/worker/orchestration/delivery/verifier).
SERVICE_RESOLVERS: dict[str, tuple[object, ...]] = {
    "gateway": (paths.database_path, paths.health_dir, paths.tasks_dir),
    "bot": (paths.turn_ledger_path, paths.voice_cache_dir, paths.downloads_dir,
            paths.voice_settings_file, paths.database_path),
    "worker": (paths.tasks_dir, paths.workspace_dir, paths.memory_db_path),
    "orchestration": (paths.health_dir, paths.tasks_dir),
    "delivery": (paths.health_dir, paths.database_path),
    "verifier": (paths.health_dir, paths.workspace_dir, paths.tasks_dir),
    # The CLI is a first-class surface: it owns the flow-tracking state, the
    # audit store, the elevation/PIN gate and the persisted aliases/themes.
    "cli": (paths.cli_state_file, paths.cli_aliases_file, paths.cli_theme_file,
            paths.cli_user_themes_file, paths.audit_log_db, paths.elevation_db,
            paths.owner_pin_file, paths.traces_db, paths.traces_log),
}

_ALL_RUNTIME_ENVS = (
    "ANTIGONA_STATE_ROOT",
    "ANTIGONA_TELEGRAM_TURN_LEDGER",
    "ANTIGONA_TASKS_DIR",
    "ANTIGONA_HEALTH_DIR",
    "ANTIGONA_WORKSPACE",
    "ANTIGONA_MEMORY_ROOT",
    "ANTIGONA_MEMORY_DIR",
    "ANTIGONA_MEMORY_DB_PATH",
    "ANTIGONA_SESSION_DB_PATH",
    "ANTIGONA_SECURITY_DIR",
    "ANTIGONA_DOWNLOADS_DIR",
    "ANTIGONA_VOICE_CACHE_DIR",
    "ANTIGONA_VOICE_SETTINGS_FILE",
    "ANTIGONA_REPLY_MAP_FILE",
    "ANTIGONA_CONTEXT_DIR",
    "ANTIGONA_CHANNEL_LOG",
    "ANTIGONA_PROVIDER_STATE_FILE",
    "ANTIGONA_LEGACY_REACHABILITY_LOG",
    "ANTIGONA_DATABASE_URL",
    "ANTIGONA_IMMUTABLE_DEPLOYMENT",
    "ANTIGONA_TRACES_DB",
    "ANTIGONA_TRACES_LOG",
    "ANTIGONA_STATE_FILE",
    "ANTIGONA_MCP_REGISTRY_FILE",
    "ANTIGONA_AUDIT_DB_PATH",
    "ANTIGONA_ELEVATION_DB_PATH",
    "ANTIGONA_OWNER_PIN_FILE",
    "ANTIGONA_OWNERSHIP_DIR",
    "ANTIGONA_CLI_ALIASES_FILE",
    "ANTIGONA_CLI_THEME_FILE",
    "ANTIGONA_CLI_USER_THEMES_FILE",
    "ANTIGONA_EVIDENCE_DIR",
    "ANTIGONA_EVIDENCE_ROOT",
)


@pytest.fixture()
def immutable_deployment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Immutable (read-only) code root with a writable state root outside it."""
    for name in _ALL_RUNTIME_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTIGONA_IMMUTABLE_DEPLOYMENT", "1")
    state_root = tmp_path / "var-lib-antigona"
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(state_root))
    return state_root


@pytest.fixture()
def dev_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Development/test checkout: no state root, no immutable marker.

    ``HOME`` is isolated so ``owner_dir()`` is a genuine owner-level state
    directory, not a code checkout.  A host that happens to have the hardened
    install at ``$HOME/.antigona`` must fail closed for owner-class stores
    (see ``tests/unit/test_code_root_write_guard.py``), which is a different
    contract from the intentional dev/test default this fixture asserts.
    """
    for name in _ALL_RUNTIME_ENVS:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".antigona").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))


def test_no_runtime_path_resolves_under_code_root_in_immutable_mode(
    immutable_deployment: Path,
) -> None:
    code_root = paths.project_root()
    for name, resolver in RUNTIME_RESOLVERS.items():
        resolved = Path(resolver())  # type: ignore[operator]
        assert not str(resolved).startswith(str(code_root) + "/"), (
            f"{name} resolved under the read-only code root: {resolved}"
        )
        assert str(resolved).startswith(str(immutable_deployment)), (
            f"{name} did not resolve under the state root: {resolved}"
        )


def test_service_surfaces_never_resolve_under_code_root(
    immutable_deployment: Path,
) -> None:
    code_root = paths.project_root()
    for service, resolvers in SERVICE_RESOLVERS.items():
        for resolver in resolvers:
            resolved = Path(resolver())  # type: ignore[operator]
            assert not str(resolved).startswith(str(code_root) + "/"), (
                f"{service} resolved a runtime path under the code root: {resolved}"
            )


def test_immutable_artifacts_stay_in_the_code_root(immutable_deployment: Path) -> None:
    code_root = paths.project_root()
    for name, resolver in IMMUTABLE_ARTIFACTS.items():
        resolved = Path(resolver())  # type: ignore[operator]
        assert str(resolved).startswith(str(code_root)), (
            f"{name} is an immutable artifact and must resolve under the code root"
        )


def test_fail_closed_without_state_root_in_immutable_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _ALL_RUNTIME_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTIGONA_IMMUTABLE_DEPLOYMENT", "1")
    for name, resolver in RUNTIME_RESOLVERS.items():
        with pytest.raises(RuntimeError) as excinfo:
            resolver()  # type: ignore[operator]
        assert "ANTIGONA_STATE_ROOT" in str(excinfo.value), (
            f"{name} fail-closed error is not actionable: {excinfo.value}"
        )


def test_dev_defaults_preserved(dev_checkout: None) -> None:
    root = paths.project_root()
    assert paths.tasks_dir() == root / ".tasks"
    assert paths.turn_ledger_path() == root / ".tasks" / "telegram_turns.db"
    assert paths.reply_map_file() == root / ".task_messages" / "messages.json"
    assert paths.voice_cache_dir() == root / ".voice_cache"
    assert paths.voice_settings_file() == root / ".voice_settings.json"
    assert paths.downloads_dir() == root / "downloads"
    assert paths.memory_db_path() == root / "antigona_memory.db"
    assert paths.workspace_dir() == root / "workspace"
    assert paths.health_dir() == root / ".health"
    assert paths.database_path() == root / "antigona.db"
    assert paths.sessions_db_path() == root / "antigona_sessions.db"
    # Owner-class runtime state keeps its intentional dev/test default too.
    owner = paths.owner_dir()
    assert paths.traces_db() == owner / "traces.db"
    assert paths.traces_log() == owner / "traces.jsonl"
    assert paths.cli_state_file() == owner / "cli_state.json"
    assert paths.mcp_registry_file() == owner / "mcp_servers.json"
    assert paths.audit_log_db() == owner / "audit_log.db"
    assert paths.elevation_db() == owner / "elevation.db"
    assert paths.owner_pin_file() == owner / "owner_pin.json"
    assert paths.ownership_db_dir() == owner / "ownership"
    assert paths.cli_aliases_file() == owner / "cli_aliases.json"
    assert paths.cli_theme_file() == owner / "cli_theme.json"
    assert paths.cli_user_themes_file() == owner / "cli_user_themes.json"
    assert paths.evidence_dir() == Path("/var/lib/antigona/evidence")


def test_dedicated_overrides_win_over_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTIGONA_IMMUTABLE_DEPLOYMENT", "1")
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(tmp_path / "state"))
    ledger = tmp_path / "dedicated" / "turns.db"
    monkeypatch.setenv("ANTIGONA_TELEGRAM_TURN_LEDGER", str(ledger))
    assert paths.turn_ledger_path() == ledger.resolve()

    workspace = tmp_path / "dedicated-workspace"
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
    assert paths.workspace_dir() == workspace.resolve()


def test_turn_ledger_default_is_governed_not_code_root(
    immutable_deployment: Path,
) -> None:
    assert paths.turn_ledger_path() == (
        immutable_deployment / ".tasks" / "telegram_turns.db"
    )


def test_turn_ledger_persists_across_restart(tmp_path: Path) -> None:
    """A completed turn replays after the owning process is replaced."""
    db = tmp_path / "ledger" / "telegram_turns.db"
    ensure_ledger_writable(db)

    async def _run() -> ClaimOutcome:
        first = TurnLedger(db, owner_token="proc-1")
        claim = await first.claim("chat:1:msg:9", 1)
        assert claim.outcome is ClaimOutcome.CLAIMED
        await first.complete("chat:1:msg:9", {"answer": 42})
        await first.close()
        assert db.is_file()

        # Simulate a restart: a brand new process/owner token opens the ledger.
        second = TurnLedger(db, owner_token="proc-2")
        replay = await second.claim("chat:1:msg:9", 1)
        await second.close()
        return replay.outcome

    assert asyncio.run(_run()) is ClaimOutcome.REPLAY


def test_unwritable_ledger_fails_closed_with_actionable_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError) as excinfo:
        ensure_ledger_writable(blocker / "telegram_turns.db")
    message = str(excinfo.value)
    assert "ANTIGONA_STATE_ROOT" in message or "ANTIGONA_TELEGRAM_TURN_LEDGER" in message


def test_audit_store_fails_closed_instead_of_relocating_to_tmp() -> None:
    """An unwritable audit store must NEVER be silently moved to /tmp.

    Silently relocating the audit trail weakens it; the governed resolver fails
    closed instead, with an actionable error naming the runtime-root env var.
    """
    import sys

    import pytest as _pytest

    from antigona.security.audit import SystemAuditLogger

    if not sys.platform.startswith("linux"):
        _pytest.skip("needs a read-only kernel filesystem")
    with _pytest.raises(RuntimeError) as excinfo:
        SystemAuditLogger("/sys/kernel/antigona_audit_probe.db")
    message = str(excinfo.value)
    assert "ANTIGONA_STATE_ROOT" in message or "ANTIGONA_AUDIT_DB_PATH" in message
