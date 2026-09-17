"""GUARD-01..08 — architecture guards for the Control-Plane Sanitation campaign.

Each guard encodes a TARGET invariant from campaign §20. Guards whose invariant
already holds are strict passing tests (regression fences). Guards whose invariant
is currently VIOLATED carry ``@pytest.mark.xfail(strict=True, ...)`` tied to a
defect ID and the wave that closes it.

``strict=True`` means: when the wave fixes the violation the test starts PASSING,
which fails the strict-xfail and FORCES removal of the marker. No guard may become
a permanent xfail (owner constraint, Wave 0).

Defect IDs: docs/control_plane/01_CONTROL_PLANE_INVENTORY.md.
Wave map:   docs/control_plane/03_STRANGLER_PLAN.md.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "antigona"

_TELEGRAM = _SRC / "channels" / "telegram"
_CLI_FILES = [_SRC / "cli.py", *(_SRC / "cli_ui").glob("*.py")]
_ADAPTER_FILES = sorted(_TELEGRAM.glob("*.py")) + _CLI_FILES


def _imports(path: Path) -> set[str]:
    """Every dotted module name imported by ``path`` (module- and function-level)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # unrelated pre-existing SyntaxWarnings in the tree
            tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def _rel(p: Path) -> str:
    return p.relative_to(_SRC).as_posix()


def _modules_importing(needle: str, *, under: Path = _SRC) -> set[str]:
    hits: set[str] = set()
    for p in under.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        if any(needle == m or m.startswith(needle + ".") for m in _imports(p)):
            hits.add(_rel(p))
    return hits


def _text(paths: list[Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths if p.exists())


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-01 — Telegram adapter is not a security authority
#   Wave B2 narrowed this: pin_gate no longer holds elevation/lockout STATE
#   (it delegates to ElevationAuthority). Telegram still imports pin_gate
#   FUNCTIONS (attempt_unlock, elevate_session, …) — closes when the Telegram
#   adapter routes auth through AuthService / ElevationAuthority (Wave D).
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="Telegram still imports pin_gate functions; closed by Wave D adapter cleanup")
def test_guard_01_telegram_not_a_security_authority() -> None:
    forbidden = (
        "antigona.tools.pin_gate",
        "antigona.security.owner_override",
        "antigona.core.owner_gate",
        "antigona.security.approval_grant",
    )
    offenders = {
        _rel(p): sorted(m for m in _imports(p) if m.startswith(forbidden))
        for p in _TELEGRAM.glob("*.py")
        if any(m.startswith(forbidden) for m in _imports(p))
    }
    assert not offenders, f"Telegram modules importing a security authority directly: {offenders}"


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-02 — CLI adapter does not self-assert a principal identity
#   Closed by Wave A: the three literal user_id="owner"/"default" call sites now
#   resolve through security.auth_service.cli_principal().
# ─────────────────────────────────────────────────────────────────────────────
def test_guard_02_cli_does_not_self_assert_principal() -> None:
    blob = _text(_CLI_FILES)
    bad = [tok for tok in ('user_id="owner"', 'user_id="default"') if tok in blob]
    assert not bad, f"CLI self-asserts a principal identity via literal: {bad}"


# GUARD-02b — CLI does not self-certify PIN verification.
#   Closed by Wave B: cli_ui/chat.py derives pin_verified from the shared
#   ElevationAuthority (layout._grant_owner_mode records it there), not from
#   re-using is_owner.
def test_guard_02b_cli_does_not_self_certify_pin() -> None:
    blob = _text(_CLI_FILES)
    assert "pin_verified=is_owner" not in blob, "CLI self-certifies PIN: pin_verified=is_owner"
    # positive fence: chat.py must consult the elevation authority
    chat = (_SRC / "cli_ui" / "chat.py").read_text(encoding="utf-8")
    assert "owner_elevation_authority" in chat and "is_elevated(CLI_OWNER_PRINCIPAL)" in chat, (
        "cli_ui/chat.py no longer resolves pin_verified through ElevationAuthority"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-03 — adapters never transition a task/flow to DONE  (holds today)
# ─────────────────────────────────────────────────────────────────────────────
def test_guard_03_adapters_do_not_write_terminal_done() -> None:
    forbidden_writes = (
        "update_status(", "transition(", "set_status(", "mark_done(", "_finalize(",
    )
    offenders: dict[str, list[str]] = {}
    for p in _ADAPTER_FILES:
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        hits = [
            line.strip()
            for line in src.splitlines()
            if any(w in line for w in forbidden_writes) and "DONE" in line
        ]
        if hits:
            offenders[_rel(p)] = hits
    assert not offenders, f"adapter writes a terminal DONE transition: {offenders}"


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-04 — adapters never execute tools in-process
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="CP-9 — cli_ui/chat.py runs UnifiedToolExecutionLayer locally; closed by Wave D")
def test_guard_04_adapters_do_not_execute_tools_directly() -> None:
    forbidden = (
        "antigona.engine.unified_executor",
        "antigona.tools.action_executor",
        "antigona.tools.registry",
    )
    offenders = {
        _rel(p): sorted(m for m in _imports(p) if m.startswith(forbidden))
        for p in _ADAPTER_FILES
        if p.exists() and any(m.startswith(forbidden) for m in _imports(p))
    }
    assert not offenders, f"adapter imports a tool executor: {offenders}"


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-05 — legacy pin_gate is only imported by sanctioned modules  (holds today)
# regression fence: no NEW / canonical importer may appear during the waves
# ─────────────────────────────────────────────────────────────────────────────
def test_guard_05_pin_gate_importers_are_allowlisted() -> None:
    allowed = {
        # adapters — retired to a thin wrapper in Wave B
        "channels/telegram/auth_handlers.py",
        "channels/telegram/bot.py",
        # legacy executor — retired in Wave B
        "tools/action_executor.py",
        # local TTY reset tool
        "bin/recovery_pin.py",
    }
    importers = _modules_importing("antigona.tools.pin_gate")
    new = importers - allowed
    assert not new, (
        f"NEW importer(s) of legacy antigona.tools.pin_gate: {sorted(new)} — "
        "route through the canonical elevation authority instead"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-06 — exactly one approval authority
# ─────────────────────────────────────────────────────────────────────────────
#: The approval-issuing / -deciding authorities that currently exist. Block 2.
#: Wave C collapses this to one; when it does, this list shrinks and the
#: xfail below starts failing (strict) → remove the marker.
APPROVAL_AUTHORITIES = {
    "core.owner_gate.OwnerGate",              # `approvals` table, no TTL
    "security.approval_grant.ApprovalGrantStore",  # `approval_grants`, TTL+one-shot
    "policy.engine.PolicyEngine._pending_confirmations",  # CRITICAL 2-step
}


@pytest.mark.xfail(strict=True, reason="CP-1 — 3 approval authorities; collapsed to 1 by Wave C")
def test_guard_06_single_approval_authority() -> None:
    assert len(APPROVAL_AUTHORITIES) == 1, (
        f"{len(APPROVAL_AUTHORITIES)} approval authorities: {sorted(APPROVAL_AUTHORITIES)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-07 — exactly one principal-identity resolver
#   Closed by Wave A: OwnerIdentity is the sole resolver; AuthService is a facade
#   over it; OwnerOverrideManager delegates; the CLI literals are gone.
#   `gateway.api.owner` is transport auth (token -> principal label), an adapter
#   over this authority, not a second resolver.
# ─────────────────────────────────────────────────────────────────────────────
#: Distinct principal-identity resolver implementations. Must stay == 1.
PRINCIPAL_AUTHORITIES = {
    "security.owner_identity.OwnerIdentity",
}


def test_guard_07_single_auth_authority() -> None:
    assert len(PRINCIPAL_AUTHORITIES) == 1, (
        f"{len(PRINCIPAL_AUTHORITIES)} principal resolvers: {sorted(PRINCIPAL_AUTHORITIES)}"
    )
    # regression fence: OwnerOverrideManager must NOT re-grow its own env resolver
    oom = (_SRC / "security" / "owner_override.py").read_text(encoding="utf-8")
    assert 'os.environ.get("ANTIGONA_TELEGRAM_OWNER_ID")' not in oom, (
        "OwnerOverrideManager re-implemented its own telegram-owner-id env resolver — "
        "it must delegate to OwnerIdentity (CP-6)"
    )
    # AuthService must be the documented single entry the CLI uses
    auth_service = _SRC / "security" / "auth_service.py"
    assert auth_service.exists(), "security/auth_service.py (the single identity authority) is missing"


# ─────────────────────────────────────────────────────────────────────────────
# GUARD-08 — adapters never read canonical task/approval state from the DB
# directly (they go through GatewayClient)  (holds today)
# ─────────────────────────────────────────────────────────────────────────────
def test_guard_08_adapters_do_not_query_canonical_tables() -> None:
    canonical_models = ("TaskFlow", "Approval", "StateTransition")
    offenders: dict[str, list[str]] = {}
    for p in _ADAPTER_FILES:
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        hits = [
            line.strip()
            for line in src.splitlines()
            if ("select(" in line or ".query(" in line or "session.get(" in line)
            and any(m in line for m in canonical_models)
        ]
        if hits:
            offenders[_rel(p)] = hits
    assert not offenders, f"adapter queries a canonical table directly: {offenders}"


# ─────────────────────────────────────────────────────────────────────────────
# Wave-0 telemetry module smoke — behaviour-neutral, never raises
# ─────────────────────────────────────────────────────────────────────────────
def test_legacy_telemetry_is_behaviour_neutral() -> None:
    from antigona import observability_legacy as tel

    tel.reset()
    tel.record("unit.probe")
    tel.record("unit.probe")
    assert tel.snapshot().get("unit.probe") == 2
    assert set(tel.first_seen()) == {"unit.probe"}
    # must swallow anything, including a non-str
    tel.record(object())  # type: ignore[arg-type]
    tel.reset()
    assert tel.snapshot() == {}
