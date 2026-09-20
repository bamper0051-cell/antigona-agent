"""Unified Paths API — единственный механизм разрешения путей Antigona.

ADR-007 (Runtime Directory / Configuration). В бизнес-логике ЗАПРЕЩЕНО:
- ``Path.home()`` / ``expanduser()``;
- хардкод абсолютных путей проекта (``/opt/antigona`` и т.п.);
- самостоятельное вычисление ``PROJECT_ROOT``;
- cwd-относительные пути конфигурации/состояния.

Все пути разрешаются ИСКЛЮЧИТЕЛЬНО через функции/константы этого модуля.
Единственное хардкод-определение корня живёт здесь (``_PROJECT_ROOT``).

Механизм приоритета: ``env`` (ANTIGONA_*) > канонический дефолт. Для
тестов допустимо переопределить корень через ``ANTIGONA_PROJECT_ROOT``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

# ── Canonical project root (единственное определение корня, ADR-007) ─────────
# Корень выводится из расположения пакета (editable/src-layout: paths.py ->
# core -> antigona -> src -> корень проекта), а не хардкодится. Приоритет:
# ANTIGONA_PROJECT_ROOT (env) > резолв из cwd (при наличии pyproject.toml) > резолв из __file__ > исторический дефолт.

def _detect_project_root() -> Path:
    env_root = os.environ.get("ANTIGONA_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists():
        return cwd
    return Path(__file__).resolve().parents[3]


_PROJECT_ROOT = _detect_project_root()


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser().resolve()
    return default


def project_root() -> Path:
    """Canonical Project Root. Overridable via ``ANTIGONA_PROJECT_ROOT``."""
    raw = os.environ.get("ANTIGONA_PROJECT_ROOT")
    return Path(raw).expanduser().resolve() if raw else _detect_project_root()



def runtime_dir() -> Path:
    """Runtime Directory — там, где живут процессы/БД/.env (== project root)."""
    return project_root()


# ── Home directory (sandbox / credential bind sources) ───────────────────────
# The bubblewrap sandbox in ``orchestration/autonomy.py`` derives its credential
# bind sources from the *current user's* home directory (``~/.codex/auth.json``,
# ``~/.local/bin``, …). That path must never be hardcoded per user: on any other
# machine the bind source simply does not exist and ``bwrap`` refuses to start
# ("Can't find source path"). Resolution lives here so no business module needs
# ``Path.home()`` / ``expanduser()`` (ADR-007).

#: Env override for the home directory used to derive sandbox bind sources.
HOME_DIR_ENV = "ANTIGONA_HOME_DIR"


def home_dir() -> Path:
    """Current user's home directory, overridable via ``ANTIGONA_HOME_DIR``.

    Returns ``Path.home()`` by default (so behaviour on the canonical host with
    ``HOME=<host-root>`` is unchanged) and the ``~``-expanded value of
    ``ANTIGONA_HOME_DIR`` when that env var is set. Never a hardcoded personal
    path.
    """
    raw = os.environ.get(HOME_DIR_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home()


# ── Canonical HIGH-risk zone anchors (never derived from ambient HOME) ────────
#
# A risk zone must not be a function of the *process* environment.  A hardened
# unit runs with ``HOME=/var/lib/antigona`` (its state root) while the deployment
# it protects — the installed code root and the owner directory next to it —
# lives under ``<host-root>``.  Deriving the HIGH zone from ``Path.home()`` therefore
# REMOVED ``<host-root>/**`` from the protected set in exactly that configuration
# (defect A-CORE-001: the same write was HIGH with ``HOME=<host-root>`` and MEDIUM with
# the live unit's ``HOME``).  The anchors below are resolved from the canonical
# deployment/state configuration instead, so an ambient ``HOME`` can only ever
# ADD a zone (fail-closed), never shrink one.

#: Env: ``os.pathsep``-separated list of extra HIGH-risk roots (operator intent).
HIGH_RISK_ROOTS_ENV = "ANTIGONA_HIGH_RISK_ROOTS"


def deployment_anchor_root() -> Path:
    """Directory that owns the installed code root — the deployment anchor.

    ``<host-root>`` for the canonical install at ``/opt/antigona-canon-*``, the owner
    home in a normal development checkout.  Derived from the code root's real
    location, never from ``Path.home()``.
    """
    return project_root().parent


def security_anchor_roots() -> tuple[Path, ...]:
    """Canonical HIGH-risk zone anchors, resolved WITHOUT the ambient ``HOME``.

    Resolution order: every ``ANTIGONA_HIGH_RISK_ROOTS`` entry (explicit operator
    intent), the deployment anchor (:func:`deployment_anchor_root`), the
    canonical deployment/code root (:func:`project_root`), an explicitly
    configured ``ANTIGONA_HOME_DIR`` and the configured runtime state root
    (``ANTIGONA_STATE_ROOT``).  The filesystem root is refused: it would classify
    every path on the host as HIGH.  Duplicates collapse; order is stable.
    """
    roots: list[Path] = []
    raw = os.environ.get(HIGH_RISK_ROOTS_ENV)
    if raw:
        for part in raw.split(os.pathsep):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser().resolve())
    roots.append(deployment_anchor_root())
    roots.append(project_root())
    explicit_home = os.environ.get(HOME_DIR_ENV)
    if explicit_home:
        roots.append(Path(explicit_home).expanduser().resolve())
    state_root = state_root_override()
    if state_root is not None:
        roots.append(state_root)

    anchors: list[Path] = []
    for root in roots:
        if root == Path("/") or not str(root).strip():
            continue
        if root not in anchors:
            anchors.append(root)
    return tuple(anchors)


def project_local_dir() -> Path:
    """Project-local agent data (``<root>/.antigona``): personalities, SOUL, AGENTS, gateway_config."""
    return project_root() / ".antigona"


def owner_dir() -> Path:
    """Owner-level Antigona data (``~/.antigona``): secrets, vault, skills, plugins, traces."""
    return Path.home() / ".antigona"


def owner_dir_is_code_root() -> bool:
    """Whether :func:`owner_dir` is actually an Antigona source checkout.

    ``owner_dir()`` (``Path.home()/'.antigona'``) is the owner-level *state*
    directory in a normal development/test environment.  In a host-root run
    against a hardened install (``HOME=<host-root>`` with the code root installed at
    ``/var/lib/antigona``) it instead points at the immutable code checkout — the
    very directory the package is installed from.  Runtime state written there
    is exactly the read-only/write-class defect this module exists to prevent,
    so the owner-class resolvers fail closed in that case instead of silently
    landing a runtime file inside the code root.
    """
    od = owner_dir()
    return (od / "pyproject.toml").is_file() and (od / "src" / "antigona" / "__init__.py").is_file()


# ── Runtime file-memory root (mutable state, never an immutable code root) ──
#
# File memory (MEMORY.md / USER.md) is runtime state. In a hardened deployment
# the code root is mounted read-only (systemd ``ProtectSystem=strict``), so the
# memory root MUST live under a writable runtime root. The only exception —
# intentionally preserved — is a development/test checkout, where the legacy
# ``<project_root>/.memory`` location stays in use.

#: Env vars that explicitly pin the runtime file-memory root (highest priority).
MEMORY_ROOT_ENVS: tuple[str, ...] = ("ANTIGONA_MEMORY_ROOT", "ANTIGONA_MEMORY_DIR")

#: Marker for an installed deployment whose code root is read-only.
IMMUTABLE_DEPLOYMENT_ENV = "ANTIGONA_IMMUTABLE_DEPLOYMENT"

_MEMORY_SUBDIR = ".memory"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_immutable_deployment() -> bool:
    """Return whether Antigona runs from an installed/read-only code root.

    Indicated explicitly by ``ANTIGONA_IMMUTABLE_DEPLOYMENT`` (truthy value).
    In that mode a writable runtime root is mandatory: file memory must never
    silently fall back to the code root.
    """
    raw = os.environ.get(IMMUTABLE_DEPLOYMENT_ENV)
    return (raw or "").strip().lower() in _TRUTHY


def memory_root_override() -> Path | None:
    """Explicitly configured runtime memory root, or ``None`` when unset.

    Priority: ``ANTIGONA_MEMORY_ROOT`` / ``ANTIGONA_MEMORY_DIR`` (dedicated)
    then the shared ``ANTIGONA_STATE_ROOT`` (as ``<state_root>/.memory``).
    ``None`` means the caller should use the development default.
    """
    for name in MEMORY_ROOT_ENVS:
        raw = os.environ.get(name)
        if raw:
            return Path(raw).expanduser().resolve()
    state_raw = os.environ.get("ANTIGONA_STATE_ROOT")
    if state_raw:
        return Path(state_raw).expanduser().resolve() / _MEMORY_SUBDIR
    return None


def memory_root() -> Path:
    """Runtime file-memory root (MEMORY.md / USER.md).

    Resolution order:

    1. ``ANTIGONA_MEMORY_ROOT`` / ``ANTIGONA_MEMORY_DIR`` (dedicated override);
    2. ``ANTIGONA_STATE_ROOT`` → ``<state_root>/.memory``;
    3. immutable deployment → ``RuntimeError`` (fail closed: the code root is
       read-only and must never be used silently);
    4. development/test default → ``<project_root>/.memory`` (unchanged).
    """
    override = memory_root_override()
    if override is not None:
        return override
    if is_immutable_deployment():
        raise RuntimeError(
            "runtime file-memory root is unset in an immutable deployment: "
            "configure ANTIGONA_STATE_ROOT (or ANTIGONA_MEMORY_ROOT) to a "
            "writable path outside the read-only code root"
        )
    return project_root() / _MEMORY_SUBDIR


def memory_dir() -> Path:
    """File-based memory dir (MEMORY.md / USER.md) — see :func:`memory_root`."""
    return memory_root()


# ── Governed runtime state root (single resolver for runtime-writable paths) ──
#
# Every path that a *running* service creates, locks or writes must resolve
# through :func:`runtime_path`. In a hardened deployment the code root
# (``/var/lib/antigona``) is mounted read-only (systemd ``ProtectSystem=strict``),
# so a runtime path that defaults under it is a latent ``OSError: [Errno 30]``.
# The runtime root therefore comes from ``ANTIGONA_STATE_ROOT`` (or a dedicated
# per-class override) and resolution FAILS CLOSED with an actionable error when
# an immutable deployment has no writable root — a durable feature must never
# silently degrade (e.g. "continuing without recovery") because a path was
# unwritable.
#
# Intentional development/test defaults are preserved: with neither a state
# root nor the immutable marker the legacy in-tree location still applies.

#: Env that pins the single writable runtime state root (highest priority).
STATE_ROOT_ENV = "ANTIGONA_STATE_ROOT"



def state_root_override() -> Path | None:
    """Explicitly configured runtime state root, or ``None`` when unset."""
    raw = os.environ.get(STATE_ROOT_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return None


def runtime_path(
    dedicated_envs: tuple[str, ...],
    state_relpath: str,
    dev_default: Path | Callable[[], Path],
    *,
    what: str,
    owner_class: bool = False,
) -> Path:
    """Resolve one runtime-writable path through the governed state root.

    Resolution order:

    1. any of *dedicated_envs* (a per-class override such as
       ``ANTIGONA_TELEGRAM_TURN_LEDGER``);
    2. ``ANTIGONA_STATE_ROOT`` → ``<state_root>/<state_relpath>``;
    3. immutable deployment without a state root → ``RuntimeError`` (fail
       closed: the code root is read-only and must never be used silently);
    4. *owner_class* resolver whose dev default would land inside an Antigona
       code checkout (``owner_dir()`` collapsed onto the installed code root,
       e.g. a host-root CLI run) → ``RuntimeError`` (fail closed: a runtime
       store must never be created in the code root);
    5. development/test default → *dev_default* (intentional, unchanged).
    """
    for name in dedicated_envs:
        raw = os.environ.get(name)
        if raw:
            return Path(raw).expanduser().resolve()
    state_root = state_root_override()
    if state_root is not None:
        return state_root / state_relpath
    if is_immutable_deployment():
        raise RuntimeError(
            f"{what} has no writable runtime root in an immutable deployment: "
            f"set {STATE_ROOT_ENV} to a writable directory outside the "
            f"read-only code root"
            + (
                f" (or override with {', '.join(dedicated_envs)})"
                if dedicated_envs
                else ""
            )
        )
    if owner_class and owner_dir_is_code_root():
        # A host-root run (HOME pointing at the installed code root) has no
        # writable runtime root and would otherwise create this store inside the
        # immutable code checkout.  Fail closed with an actionable error instead.
        raise RuntimeError(
            f"{what} has no writable runtime root: it would resolve inside the "
            f"Antigona code root ({owner_dir()}), which is not a runtime state "
            f"location. Set {STATE_ROOT_ENV} to a writable directory outside "
            f"the code root"
            + (
                f" (or override with {', '.join(dedicated_envs)})"
                if dedicated_envs
                else ""
            )
        )
    # *dev_default* may be a callable so the (potentially cwd-inspecting)
    # project root is only resolved when the legacy dev/test default is used.
    return dev_default() if callable(dev_default) else dev_default


def tasks_dir() -> Path:
    """Persistent task/recovery state (runtime-writable).

    Carries the durable task JSON, event log, recovery checkpoints and the
    Telegram exactly-once turn ledger.
    """
    return runtime_path(
        ("ANTIGONA_TASKS_DIR",),
        ".tasks",
        lambda: project_root() / ".tasks",
        what="tasks/recovery state directory",
    )


def turn_ledger_path() -> Path:
    """Durable Telegram exactly-once turn ledger (runtime-writable).

    ``ANTIGONA_TELEGRAM_TURN_LEDGER`` > ``<state_root>/.tasks/telegram_turns.db``
    → fail closed in immutable mode → dev default ``<tasks_dir>/telegram_turns.db``.
    """
    raw = os.environ.get("ANTIGONA_TELEGRAM_TURN_LEDGER")
    if raw:
        return Path(raw).expanduser().resolve()
    return tasks_dir() / "telegram_turns.db"


def health_dir() -> Path:
    """Per-service heartbeat directory written by services and read by Gateway."""
    return runtime_path(
        ("ANTIGONA_HEALTH_DIR",),
        "health",
        lambda: project_root() / ".health",
        what="health/heartbeat directory",
    )


def channel_log_file() -> Path:
    """Append-only transport/channel output log (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_CHANNEL_LOG",),
        "logs/antigona_output.log",
        lambda: project_local_dir() / "antigona_output.log",
        what="channel output log",
    )


def isolation_state_file() -> Path:
    """Runtime state file holding the last probed sandbox isolation level.

    Written by the Gateway health/status handler (and any caller of
    ``sandbox.runner.write_isolation_state``) so an external check can read the
    ACTIVE isolation level without speaking HTTP or reading code.
    """
    return runtime_path(
        ("ANTIGONA_SANDBOX_ISOLATION_STATE",),
        "sandbox_isolation.json",
        lambda: project_local_dir() / "sandbox_isolation.json",
        what="sandbox isolation state file",
    )


def provider_state_file() -> Path:
    """Persisted provider selection shared by CLI and Gateway (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_PROVIDER_STATE_FILE",),
        "provider_state.json",
        lambda: project_local_dir() / "provider_state.json",
        what="provider state file",
    )


def legacy_reachability_log() -> Path:
    """Opt-in legacy-reachability telemetry log (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_LEGACY_REACHABILITY_LOG",),
        "legacy_reachability.jsonl",
        lambda: project_root() / "legacy_reachability.jsonl",
        what="legacy reachability log",
    )


def workspace_dir() -> Path:
    """Sandbox/workspace root (env ANTIGONA_WORKSPACE, runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_WORKSPACE",),
        "workspace",
        lambda: project_root() / "workspace",
        what="workspace/sandbox root",
    )


def kanban_dir() -> Path:
    """Default file-based Kanban board root (``<workspace>/.kanban``)."""
    return workspace_dir() / ".kanban"


def security_dir() -> Path:
    """Security persistence dir (OTP/TOTP), env ANTIGONA_SECURITY_DIR (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_SECURITY_DIR",),
        "security",
        lambda: project_root() / ".security",
        what="security persistence directory",
    )


def downloads_dir() -> Path:
    """Inbound download staging root (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_DOWNLOADS_DIR",),
        "downloads",
        lambda: project_root() / "downloads",
        what="downloads directory",
    )


def evidence_dir() -> Path:
    """Runtime evidence root outside Git source (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_EVIDENCE_DIR", "ANTIGONA_EVIDENCE_ROOT"),
        "evidence",
        lambda: Path("/var/lib/antigona/evidence"),
        what="runtime evidence directory",
    )


# ── Databases ────────────────────────────────────────────────────────────────

def database_path() -> Path:
    """Primary runtime DB. From env ANTIGONA_DATABASE_URL (sqlite:///...) else ``<root>/antigona.db``."""
    raw = os.environ.get("ANTIGONA_DATABASE_URL")
    if raw and raw.startswith("sqlite:///"):
        rel = raw[len("sqlite:///"):]
        if rel.startswith("/"):
            return Path(rel).resolve()
        return (project_root() / rel).resolve()
    if raw:
        return project_root() / "antigona.db"
    return runtime_path(
        (),
        "antigona.db",
        lambda: project_root() / "antigona.db",
        what="primary runtime database",
    )


def sessions_db_path() -> Path:
    """Sessions SQLite DB. Overridable via ``ANTIGONA_SESSION_DB_PATH`` (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_SESSION_DB_PATH",),
        "antigona_sessions.db",
        lambda: project_root() / "antigona_sessions.db",
        what="sessions database",
    )


def memory_db_path() -> Path:
    """Long-term memory SQLite DB (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_MEMORY_DB_PATH",),
        "antigona_memory.db",
        lambda: project_root() / "antigona_memory.db",
        what="long-term memory database",
    )


# ── Owner-level secrets / stores ─────────────────────────────────────────────

def secrets_dir() -> Path:
    return owner_dir() / "secrets"


def vault_dir() -> Path:
    return owner_dir() / "vault"


def skills_dir() -> Path:
    return owner_dir() / "skills"


def plugins_dir() -> Path:
    return owner_dir() / "plugins"


# ── Governed "owner-class" runtime-writable state ────────────────────────────
#
# The owner-level stores below (audit DB, elevation store, traces, CLI state,
# MCP registry, aliases/theme, ownership ledgers) are RUNTIME state: a running
# service or CLI creates and rewrites them.  They used to resolve to
# ``owner_dir()`` (``~/.antigona``), which IS the read-only code root in a
# hardened install where the service runs as root — the exact same
# read-only-code-root write class as ``.memory`` / ``.tasks`` (see
# :func:`runtime_path`).  Every one of them now goes through the single
# governed resolver: dedicated env override > ``ANTIGONA_STATE_ROOT`` >
# fail-closed in immutable mode > intentional dev/test default (unchanged).
#
# NOTE: secrets/vault/skills/plugins stay owner-level *configuration* and are
# deliberately NOT relocated — they are read, never runtime-rewritten.

def traces_db() -> Path:
    """Structured dialogue-trace SQLite store (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_TRACES_DB",),
        "traces.db",
        lambda: owner_dir() / "traces.db",
        what="dialogue trace database",
        owner_class=True,
    )


def traces_log() -> Path:
    """Structured dialogue-trace JSON-lines log (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_TRACES_LOG",),
        "traces.jsonl",
        lambda: owner_dir() / "traces.jsonl",
        what="dialogue trace log",
        owner_class=True,
    )


def cli_state_file() -> Path:
    """CLI flow-tracking state (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_STATE_FILE",),
        "cli_state.json",
        lambda: owner_dir() / "cli_state.json",
        what="CLI state file",
        owner_class=True,
    )


def mcp_registry_file() -> Path:
    """Persisted MCP server registry (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_MCP_REGISTRY_FILE",),
        "mcp_servers.json",
        lambda: owner_dir() / "mcp_servers.json",
        what="MCP server registry file",
        owner_class=True,
    )


def audit_log_db() -> Path:
    """System audit-log SQLite database (runtime-writable, audit trail)."""
    return runtime_path(
        ("ANTIGONA_AUDIT_DB_PATH",),
        "audit_log.db",
        lambda: owner_dir() / "audit_log.db",
        what="system audit log database",
        owner_class=True,
    )


def elevation_db() -> Path:
    """Owner elevation / brute-force lockout store (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_ELEVATION_DB_PATH",),
        "elevation.db",
        lambda: owner_dir() / "elevation.db",
        what="owner elevation store",
        owner_class=True,
    )


def owner_pin_file() -> Path:
    """Owner PIN hash file (runtime-writable security state)."""
    return runtime_path(
        ("ANTIGONA_OWNER_PIN_FILE",),
        "owner_pin.json",
        lambda: owner_dir() / "owner_pin.json",
        what="owner PIN file",
        owner_class=True,
    )


def ownership_db_dir() -> Path:
    """Directory holding per-repo ownership ledgers (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_OWNERSHIP_DIR",),
        "ownership",
        lambda: owner_dir() / "ownership",
        what="ownership ledger directory",
        owner_class=True,
    )


def cli_aliases_file() -> Path:
    """Persisted CLI slash-command aliases (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_CLI_ALIASES_FILE",),
        "cli_aliases.json",
        lambda: owner_dir() / "cli_aliases.json",
        what="CLI aliases file",
        owner_class=True,
    )


def cli_theme_file() -> Path:
    """Persisted active CLI theme (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_CLI_THEME_FILE",),
        "cli_theme.json",
        lambda: owner_dir() / "cli_theme.json",
        what="CLI theme file",
        owner_class=True,
    )


def cli_user_themes_file() -> Path:
    """Persisted user-defined CLI themes (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_CLI_USER_THEMES_FILE",),
        "cli_user_themes.json",
        lambda: owner_dir() / "cli_user_themes.json",
        what="CLI user themes file",
        owner_class=True,
    )


# ── Project-local files (personality / tool config) ──────────────────────────

def personalities_dir() -> Path:
    return project_local_dir() / "personalities"


def soul_file() -> Path:
    return project_local_dir() / "SOUL.md"


def agents_file() -> Path:
    return project_local_dir() / "AGENTS.md"


def gateway_config_path() -> Path:
    return project_local_dir() / "gateway_config.json"


# ── Runtime files ────────────────────────────────────────────────────────────

def env_file() -> Path:
    return project_root() / ".env"


def pid_file() -> Path:
    return _env_path("ANTIGONA_PID_FILE", Path("/tmp/antigona_bot.pid"))


def tasks_state_file() -> Path:
    return tasks_dir() / "tasks.json"


def recovery_checkpoint_file() -> Path:
    return tasks_dir() / "recovery_checkpoints.json"


def learnings_file() -> Path:
    return memory_dir() / "learnings.json"


def reply_map_file() -> Path:
    """Reply-mapping store (message_id → task_id), runtime-writable."""
    return runtime_path(
        ("ANTIGONA_REPLY_MAP_FILE",),
        ".task_messages/messages.json",
        lambda: project_root() / ".task_messages" / "messages.json",
        what="reply-mapping store",
    )


def context_chat_file(chat_id: int) -> Path:
    """Per-chat memory summarizer persistence (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_CONTEXT_DIR",),
        f".context/chat_{chat_id}.json",
        lambda: project_root() / ".context" / f"chat_{chat_id}.json",
        what="per-chat context store",
    )


def voice_cache_dir() -> Path:
    """Voice TTS cache directory (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_VOICE_CACHE_DIR",),
        ".voice_cache",
        lambda: project_root() / ".voice_cache",
        what="voice cache directory",
    )


def voice_settings_file() -> Path:
    """Voice settings JSON persistence (runtime-writable)."""
    return runtime_path(
        ("ANTIGONA_VOICE_SETTINGS_FILE",),
        ".voice_settings.json",
        lambda: project_root() / ".voice_settings.json",
        what="voice settings file",
    )


def events_log_file() -> Path:
    """Task EventBus JSONL persistence."""
    return tasks_dir() / "events.jsonl"


# ── Runtime path registry (для validator / CI-гарда / самопроверки) ─────────

def canonical_roots() -> tuple[Path, ...]:
    """Официальные корни, внутри которых живут все runtime-ресурсы."""
    return (project_root(), owner_dir(), project_local_dir(), workspace_dir())


def all_resolved_paths() -> dict[str, str]:
    """Карта имя -> абсолютный путь для архитектурной самопроверки."""
    items = {
        "project_root": project_root(),
        "runtime_dir": runtime_dir(),
        "project_local_dir": project_local_dir(),
        "owner_dir": owner_dir(),
        "memory_dir": memory_dir(),
        "tasks_dir": tasks_dir(),
        "workspace_dir": workspace_dir(),
        "security_dir": security_dir(),
        "database_path": database_path(),
        "sessions_db": sessions_db_path(),
        "memory_db": memory_db_path(),
        "secrets_dir": secrets_dir(),
        "vault_dir": vault_dir(),
        "skills_dir": skills_dir(),
        "plugins_dir": plugins_dir(),
        "traces_db": traces_db(),
        "traces_log": traces_log(),
        "cli_state": cli_state_file(),
        "mcp_registry": mcp_registry_file(),
        "audit_log_db": audit_log_db(),
        "elevation_db": elevation_db(),
        "owner_pin_file": owner_pin_file(),
        "ownership_db_dir": ownership_db_dir(),
        "cli_aliases": cli_aliases_file(),
        "cli_theme": cli_theme_file(),
        "cli_user_themes": cli_user_themes_file(),
        "personalities_dir": personalities_dir(),
        "soul_file": soul_file(),
        "agents_file": agents_file(),
        "gateway_config": gateway_config_path(),
        "env_file": env_file(),
        "pid_file": pid_file(),
        "reply_map": reply_map_file(),
        "events_log": events_log_file(),
        "voice_cache": voice_cache_dir(),
        "voice_settings": voice_settings_file(),
        "turn_ledger": turn_ledger_path(),
        "health_dir": health_dir(),
        "channel_log": channel_log_file(),
        "provider_state": provider_state_file(),
        "evidence_dir": evidence_dir(),
    }
    return {k: str(v) for k, v in items.items()}
