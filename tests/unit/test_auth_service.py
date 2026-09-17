"""Wave A — AuthService: one principal-identity authority (findings CP-3, CP-6).

RED before Wave A: security/auth_service.py did not exist; the CLI hard-coded
user_id="owner"/"default"; OwnerOverrideManager read ANTIGONA_TELEGRAM_OWNER_ID
via its own resolver while OwnerIdentity read only ANTIGONA_OWNER_ID.
"""

from __future__ import annotations

import pytest

from antigona.security.auth_service import AuthService, cli_principal
from antigona.security.owner_identity import OwnerIdentity
from antigona.security.owner_override import OwnerOverrideManager

_OWNER_ENVS = ("ANTIGONA_OWNER_ID", "ANTIGONA_TELEGRAM_OWNER_ID")


@pytest.fixture(autouse=True)
def _clear_owner_env(monkeypatch: pytest.MonkeyPatch):
    for name in _OWNER_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


# ── principal id ────────────────────────────────────────────────────────────


def test_owner_principal_id_from_canonical_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "12345")
    assert AuthService().owner_principal_id == "12345"


def test_owner_principal_id_fallback_is_backward_compatible() -> None:
    # no env configured -> historical literal, so a plain dev checkout is unchanged
    assert AuthService().owner_principal_id == "owner"
    assert cli_principal() == "owner"


def test_cli_principal_matches_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    assert cli_principal() == AuthService().local_cli_principal() == "42"


# ── owner check delegates to OwnerIdentity ──────────────────────────────────


def test_is_owner_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "500")
    svc = AuthService()
    assert svc.is_owner(500) is True
    assert svc.is_owner("500") is True
    assert svc.is_owner(501) is False
    assert svc.is_owner(None) is False


def test_is_owner_unconfigured_is_fail_closed() -> None:
    svc = AuthService()
    assert svc.is_owner(1) is False
    assert svc.is_owner_configured is False


# ── CP-6: one env-var handling across OwnerIdentity + OwnerOverrideManager ───


def test_telegram_owner_id_is_recognised_as_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_TELEGRAM_OWNER_ID", "999")
    assert OwnerIdentity().is_owner(999) is True
    assert AuthService().is_owner(999) is True


def test_owner_override_resolves_telegram_owner_via_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("ANTIGONA_TELEGRAM_OWNER_ID", "777")
    mgr = OwnerOverrideManager(pin_file_path=tmp_path / "pin.json")
    assert mgr.is_telegram_owner(777) is True
    assert mgr.is_telegram_owner(778) is False


def test_owner_override_and_identity_agree_on_canonical_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "555")
    identity = OwnerIdentity()
    mgr = OwnerOverrideManager(pin_file_path=tmp_path / "pin.json")
    assert identity.is_owner(555) == mgr.is_telegram_owner(555) is True
    assert identity.is_owner(556) == mgr.is_telegram_owner(556) is False


def test_owner_override_no_longer_has_its_own_env_resolver() -> None:
    import inspect

    src = inspect.getsource(OwnerOverrideManager.__init__)
    assert 'os.environ.get("ANTIGONA_TELEGRAM_OWNER_ID")' not in src


# ── CLI call sites no longer carry a literal principal ──────────────────────


def test_cli_modules_have_no_literal_principal() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "antigona"
    for rel in ("cli.py", "cli_ui/chat.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert 'user_id="owner"' not in text, rel
        assert 'user_id="default"' not in text, rel
