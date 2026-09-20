"""Read-back confirmation levels for delivery receipts (B53).

A ``delivery_receipts`` row is a *transmission* record: it says an adapter
handed an event to an external channel. That is not the same as the channel
confirming the hand-off, and it is emphatically not the same as a human reading
the message. This module names the three confirmation levels so no caller,
test or document can collapse them:

``SEND_ACK``
    The provider returned a stable identifier for the message (for example
    Telegram's ``result.message_id``). This confirms **transmission only** —
    never that the recipient read the message.

``UNSUPPORTED``
    The adapter cannot confirm anything about the provider side (mock runtime,
    or a provider that returns no identifier). It must **never** be read as
    confirmation that the message reached the recipient.

``REFUTED``
    A verification probe ran and did not find the message; the claimed
    transmission is contradicted.

``transmitted=True`` on its own stays a transmission-level flag and is *not*
confirmation of delivery to the recipient; only
:func:`read_back_confirms_transmission` decides whether a read-back level
establishes provider acknowledgement (and even then, transmission — not reading).

:func:`reconcile_read_back` is the *consumer* of those levels: it is the only
place where a stored status can be lowered (to ``REFUTED``, when a probe says the
provider does not have the message). It can never raise a level — in particular
it can never turn a non-``SEND_ACK`` status into ``SEND_ACK``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .adapter import DeliveryAdapter, ProgressEvent

#: Provider acknowledged the transmission by returning a message identifier.
READ_BACK_SEND_ACK = "SEND_ACK"
#: The adapter cannot confirm anything; never a delivery claim.
READ_BACK_UNSUPPORTED = "UNSUPPORTED"
#: A verification probe did not find the message; the claim is contradicted.
READ_BACK_REFUTED = "REFUTED"

#: The complete, closed set of levels persisted in ``delivery_receipts``.
READ_BACK_STATUSES: frozenset[str] = frozenset(
    {READ_BACK_SEND_ACK, READ_BACK_UNSUPPORTED, READ_BACK_REFUTED}
)

#: A one-argument probe that asks the provider whether a message exists.
#:
#: ``True``  — the provider confirmed the message exists (transmission level).
#: ``False`` — the provider explicitly did NOT find the message; the claimed
#:             transmission is contradicted.
#: ``None``  — the question could not be asked or the answer is unknown. This is
#:             **not evidence** of anything: never read it as confirmation and
#:             never read it as refutation.
#:
#: The Telegram Bot API has no get-message endpoint, so no live Telegram probe
#: exists; real probes come from providers that expose a lookup (or from an
#: injected probe in tests).
TransmissionProbe = Callable[[str], bool | None]


def read_back_confirms_transmission(status: str | None) -> bool:
    """Return ``True`` only for ``SEND_ACK`` — provider-acknowledged transmission.

    ``UNSUPPORTED``, ``REFUTED`` and ``None`` confirm nothing, and in particular
    are never evidence that a message reached (or was read by) the recipient.
    """
    return status == READ_BACK_SEND_ACK


def reconcile_read_back(
    provider_message_id: str | None,
    status: str | None,
    probe: TransmissionProbe | None,
) -> str | None:
    """Reconcile one stored read-back ``status`` against at most one probe call.

    A probe is asked only when there is something to confirm: the stored status
    is ``SEND_ACK`` *and* a ``provider_message_id`` exists *and* a ``probe`` was
    supplied. In that case ``probe(provider_message_id)`` runs exactly once:

    * ``False`` → ``READ_BACK_REFUTED``: the provider explicitly did not find the
      message, so the stored transmission claim is contradicted.
    * ``True`` → the stored status is returned **unchanged**. A probe only
      *preserves* an existing ``SEND_ACK``; it never creates or upgrades one, and
      ``SEND_ACK`` is provider-acknowledged *transmission* — never a read.
    * ``None`` → the stored status is returned unchanged. "Cannot probe" is
      **not evidence**: it is not a confirmation and not a refutation.

    ``status`` is returned untouched for every other input as well: a missing or
    empty ``provider_message_id`` (there is nothing to ask about, and a missing
    identifier is itself not evidence), no ``probe`` at all, and any non-``SEND_ACK``
    status. A non-``SEND_ACK`` input is **never** upgraded to ``SEND_ACK`` — this
    function can only preserve a level or downgrade it to ``REFUTED``.

    Only an explicit boolean ``False`` refutes. Any other probe answer (``None``,
    a truthy non-boolean) leaves the status alone, so an unknown probe outcome can
    never become evidence.
    """
    if status != READ_BACK_SEND_ACK:
        # Preserving is the only thing a probe can do to a non-ACK level; this
        # early return is what forbids the upgrade path outright.
        return status
    if probe is None or not provider_message_id:
        return status
    if probe(provider_message_id) is False:
        return READ_BACK_REFUTED
    return status


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of a single adapter dispatch.

    ``transmitted`` preserves the historical boolean contract of
    :meth:`DeliveryAdapter.deliver`: ``True`` means the message left the process
    toward the external channel (transmission level). ``provider_message_id`` is
    the provider's own identifier when it returned one. ``read_back_status`` is
    the confirmation level (see the module docstring) — never infer a recipient
    delivery from it.
    """

    transmitted: bool
    provider_message_id: str | None = None
    read_back_status: str = READ_BACK_UNSUPPORTED


def adapter_dispatch_outcome(
    adapter: DeliveryAdapter, event: ProgressEvent, idempotency_key: str
) -> DeliveryOutcome:
    """Dispatch through ``adapter`` while preserving the legacy boolean contract.

    An adapter may implement the optional ``deliver_outcome`` method (see
    :class:`~antigona.delivery.adapter.TelegramAdapter`) to report the provider
    message identifier and a read-back level. Adapters that only implement the
    required boolean ``deliver`` are wrapped here: their return value is kept
    verbatim and the read-back level defaults to ``UNSUPPORTED``, so a legacy
    ``True`` still asserts *transmission only*, exactly as before B53. Existing
    callers therefore keep working unchanged.
    """
    outcome = getattr(adapter, "deliver_outcome", None)
    if callable(outcome):
        result = outcome(event, idempotency_key)
        if isinstance(result, DeliveryOutcome):
            return result
        # Defensive: an adapter whose optional method returns a bare bool keeps
        # the pre-B53 transmission-only semantics rather than crashing callers.
        return DeliveryOutcome(transmitted=bool(result))
    return DeliveryOutcome(transmitted=bool(adapter.deliver(event, idempotency_key)))
