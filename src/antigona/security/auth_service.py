"""AuthService — the single principal-identity authority for Antigona.

Answers exactly two questions, for every channel, from one place:

* **who is the principal?**  → :meth:`principal_id` / :meth:`local_cli_principal`
* **is this user the owner?** → :meth:`is_owner`

It does NOT do PIN verification or elevation — that is the ElevationAuthority
(campaign Wave B). It does NOT do transport authentication (Bearer-token
matching) — that stays in ``gateway.api.owner``, which resolves a token to a
principal label and is an *adapter* over this service, not a second authority.

Before this module (campaign Block 2 / findings CP-3, CP-6):

* ``OwnerIdentity`` read ``ANTIGONA_OWNER_ID`` only;
* ``OwnerOverrideManager`` read ``ANTIGONA_TELEGRAM_OWNER_ID`` *or*
  ``ANTIGONA_OWNER_ID`` — a second resolver on a different env var;
* the CLI hard-coded ``user_id="owner"`` / ``"default"`` in three call sites,
  minting its own principal with no service in the loop.

All of that now funnels through this class.
"""

from __future__ import annotations

import logging

from antigona.security.owner_identity import OwnerIdentity

logger = logging.getLogger(__name__)

#: Principal label for a local-trusted context (interactive CLI on the owner's
#: own machine) when no numeric owner id is configured. Kept as the historical
#: literal so environments without ``ANTIGONA_OWNER_ID`` are unchanged.
LOCAL_TRUSTED_PRINCIPAL = "owner"


class AuthService:
    """One resolver for principal identity + owner check across all channels."""

    def __init__(self, owner_identity: OwnerIdentity | None = None) -> None:
        self._identity = owner_identity or OwnerIdentity()

    # ── identity ───────────────────────────────────────────────────────────

    @property
    def owner_identity(self) -> OwnerIdentity:
        return self._identity

    @property
    def owner_principal_id(self) -> str:
        """Canonical principal string for the configured owner.

        ``str(ANTIGONA_OWNER_ID)`` when set (numeric Telegram/user id), else the
        local-trusted fallback — so a plain dev checkout with no env keeps the
        old ``"owner"`` value and behaviour.
        """
        uid = self._identity.owner_user_id
        return str(uid) if uid is not None else LOCAL_TRUSTED_PRINCIPAL

    def local_cli_principal(self) -> str:
        """Principal for an interactive CLI turn.

        The CLI runs in a local-trusted context (the operator is physically at
        the owner's machine), so the principal *is* the configured owner. This
        replaces the hard-coded ``user_id="owner"`` / ``"default"`` literals —
        the value is the same in the common no-env case, but it now comes from
        one authority instead of three string literals.
        """
        return self.owner_principal_id

    # ── owner check ────────────────────────────────────────────────────────

    def is_owner(self, user_id: int | str | None) -> bool:
        """True iff ``user_id`` is the configured owner. Delegates to OwnerIdentity."""
        if user_id is None:
            return False
        try:
            return self._identity.is_owner(int(str(user_id).strip()))
        except (TypeError, ValueError):
            # non-numeric principal label: only the local-trusted fallback counts
            return str(user_id) == LOCAL_TRUSTED_PRINCIPAL and self._identity.owner_user_id is None

    @property
    def is_owner_configured(self) -> bool:
        return self._identity.is_configured


def cli_principal() -> str:
    """Module-level shortcut: the principal for an interactive CLI turn.

    Constructs a fresh :class:`AuthService` (identity is read from the
    environment on construction, mirroring every other ``OwnerIdentity()`` call
    site) so a test that patches the env is honoured.
    """
    return AuthService().local_cli_principal()
