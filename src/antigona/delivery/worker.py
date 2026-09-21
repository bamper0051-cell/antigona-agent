from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from antigona.models import DeliveryOutbox, DeliveryReceipt, utcnow

from .adapter import DeliveryAdapter, ProgressEvent, adapter_probe
from .errors import DeliveryPermanentError, sanitize_delivery_error
from .readback import (
    READ_BACK_SEND_ACK,
    READ_BACK_UNSUPPORTED,
    TransmissionProbe,
    adapter_dispatch_outcome,
    reconcile_read_back,
)
from .router import Router

if TYPE_CHECKING:
    from antigona.config import Settings

DEFAULT_MAX_ATTEMPTS = 5


class DeliveryWorker:
    """Outbox dispatch worker supporting single-adapter and multi-channel routing."""

    def __init__(
        self,
        session: Session | sessionmaker[Session],
        adapter: DeliveryAdapter | None = None,
        worker_id: str = "delivery",
        router: Router | None = None,
        settings: Settings | None = None,
        probe: TransmissionProbe | None = None,
    ) -> None:
        self._factory: sessionmaker[Session] | None = (
            session if isinstance(session, sessionmaker) else None
        )
        self._session: Session | None = session if not isinstance(session, sessionmaker) else None
        self.adapter = adapter
        self.worker_id = worker_id
        self.router = router
        self.settings = settings
        #: Optional read-back probe (B53). When ``None`` the worker asks the
        #: adapter for its own optional ``probe_message`` hook, and an adapter
        #: without one simply cannot probe — see :func:`_resolve_probe`.
        self.probe = probe

    def _resolve_probe(self, channel_name: str) -> TransmissionProbe | None:
        """Return the read-back probe to use for *channel_name*, if any.

        An explicitly injected ``probe`` always wins (the loop is exercised with
        an injected probe). Otherwise the adapter that performed this dispatch is
        asked for its optional ``probe_message`` hook; an adapter without the hook
        — e.g. :class:`~antigona.delivery.adapter.TelegramAdapter`, since the Bot
        API has no get-message endpoint — yields ``None``, meaning "cannot probe",
        which is never evidence about the message.
        """
        if self.probe is not None:
            return self.probe
        adapter = self.adapter
        if adapter is None and self.router is not None:
            try:
                adapter = self.router.get_adapter(channel_name)
            except Exception:
                return None
        if adapter is None:
            return None
        return adapter_probe(adapter)

    def _get_session(self) -> Session:
        if self._factory is not None:
            if self._session is None:
                self._session = self._factory()
        return cast(Session, self._session)

    def claim(self, lease_seconds: int = 30) -> DeliveryOutbox | None:
        now = utcnow()
        session = self._get_session()
        candidate_id = session.scalar(
            select(DeliveryOutbox.id)
            .where(
                DeliveryOutbox.available_at <= now,
                or_(
                    DeliveryOutbox.status == "PENDING",
                    (DeliveryOutbox.status == "SENDING") & (DeliveryOutbox.lease_expires_at < now),
                ),
            )
            .order_by(DeliveryOutbox.created_at)
            .limit(1)
        )
        if not candidate_id:
            return None
        result = session.execute(
            update(DeliveryOutbox)
            .where(
                DeliveryOutbox.id == candidate_id,
                or_(DeliveryOutbox.status == "PENDING", DeliveryOutbox.lease_expires_at < now),
            )
            .values(
                status="SENDING",
                lease_owner=self.worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=DeliveryOutbox.attempts + 1,
            )
        )
        assert isinstance(result, CursorResult)
        if result.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        claimed = session.get(DeliveryOutbox, candidate_id)
        return claimed

    def dispatch_one(self) -> bool:
        item = self.claim()
        if not item:
            return False
        session = self._get_session()

        # Durable crash-after-send idempotency check (P2):
        # If this idempotency_key was already transmitted to the external channel
        # by a prior worker that died before updating outbox status, do not re-send.
        receipt = session.get(DeliveryReceipt, item.idempotency_key)
        if receipt is not None:
            if receipt.transmitted:
                item.status = "DELIVERED"
                item.delivered_at = receipt.delivered_at or utcnow()
            else:
                item.status = "SIMULATED"
                item.delivered_at = None
            item.lease_owner = None
            item.lease_expires_at = None
            session.commit()
            return True

        # Durable send-confirmed marker (P2, receipt-commit crash window):
        # a prior in-flight attempt recorded ``delivered_at`` on the outbox row
        # itself right after the external channel accepted the message, then died
        # before the DeliveryReceipt row was committed. The external send already
        # happened, so re-sending would duplicate it. Honor the durable marker
        # instead of re-transmitting. Fresh PENDING rows have delivered_at=None,
        # so this only short-circuits rows confirmed sent by an earlier attempt.
        if item.delivered_at is not None:
            # The durable marker proves the external send happened but the success
            # DeliveryReceipt was never committed. Write it now (matching the
            # marker's timestamp) so the success is durable and idempotent before
            # marking DELIVERED. Path (A) above already returned when a receipt
            # exists, so this never creates a duplicate. The provider identifier
            # from the original send is not recoverable here, so the read-back
            # level is honestly UNSUPPORTED: this receipt asserts transmission
            # only and must never be read as confirmed recipient delivery.
            channel_name = item.adapter or "progress"
            session.add(
                DeliveryReceipt(
                    idempotency_key=item.idempotency_key,
                    adapter=channel_name,
                    task_id=item.task_id,
                    delivered_at=item.delivered_at,
                    transmitted=True,
                    provider_message_id=None,
                    read_back_status=READ_BACK_UNSUPPORTED,
                    read_back_at=None,
                )
            )
            item.status = "DELIVERED"
            item.lease_owner = None
            item.lease_expires_at = None
            session.commit()
            return True

        payload = item.payload
        event = ProgressEvent(
            task_id=str(payload["task_id"]),
            session_id=str(payload["session_id"]),
            correlation_id=str(payload["correlation_id"]),
            step_id=str(payload["step_id"]) if payload.get("step_id") else None,
            status=str(payload["status"]),
            message=str(payload["message"]),
            # FP-L07: the outbox row's own type decides whether this is an
            # internal transition (one live indicator, edited) or an
            # owner-facing result (one final message).
            event_type=str(item.event_type or "result"),
        )
        channel_name = item.adapter or "progress"

        # TelegramAdapter instances are recreated with each worker. Recover the
        # persisted progress bubble before dispatching the next transition.
        if event.event_type == "transition":
            adapter = self.adapter
            recover_progress_message = getattr(adapter, "recover_progress_message", None)
            if callable(recover_progress_message):
                prior_message_id = session.scalar(
                    select(DeliveryReceipt.provider_message_id)
                    .where(
                        and_(
                            DeliveryReceipt.task_id == item.task_id,
                            DeliveryReceipt.adapter == channel_name,
                            DeliveryReceipt.provider_message_id.is_not(None),
                            DeliveryReceipt.transmitted.is_(True),
                        )
                    )
                    .order_by(DeliveryReceipt.delivered_at.desc())
                    .limit(1)
                )
                if prior_message_id is not None:
                    recover_progress_message(item.task_id, prior_message_id)

        try:
            if self.router is not None:
                outcome = self.router.deliver_outcome(channel_name, event, item.idempotency_key)
            elif self.adapter is not None:
                outcome = adapter_dispatch_outcome(self.adapter, event, item.idempotency_key)
            else:
                from antigona.config import Settings

                self.settings = self.settings or Settings.from_env()
                self.router = Router(self.settings)
                outcome = self.router.deliver_outcome(channel_name, event, item.idempotency_key)
        except Exception as exc:
            max_attempts = (
                self.settings.delivery_max_attempts
                if self.settings is not None
                else DEFAULT_MAX_ATTEMPTS
            )
            # Unknown channels and configuration errors never succeed on retry;
            # everything else gets exponential backoff up to max_attempts, then
            # becomes terminal too. claim() never reclaims FAILED rows.
            terminal = isinstance(exc, DeliveryPermanentError) or item.attempts >= max_attempts
            item.status = "FAILED" if terminal else "PENDING"
            item.last_error = sanitize_delivery_error(exc)
            if not terminal:
                item.available_at = utcnow() + timedelta(seconds=min(60, 2**item.attempts))
            item.lease_owner = None
            item.lease_expires_at = None
            session.commit()
            return False

        now = utcnow()
        transmitted = outcome.transmitted
        if transmitted:
            # Durable "send confirmed" marker written BEFORE the receipt is
            # constructed/committed. If the process crashes in the window between
            # here and the receipt commit below, this survives on the outbox row
            # so a reclaiming worker sees the send already happened and does not
            # re-transmit. The row stays SENDING (an expired lease is still
            # reclaimable), it just carries delivered_at now.
            item.delivered_at = now
            session.commit()

        # B53 read-back consumer: a SEND_ACK receipt holds a provider message id
        # that was previously never read back, so no receipt could ever become
        # REFUTED. Run the optional probe ONCE against that id; a probe answering
        # False downgrades the persisted level to REFUTED. A probe answering True
        # or None (or no probe at all) leaves the stored level exactly as it was —
        # this can only preserve or lower a level, never create one. No schema
        # change is involved: the same read_back_status column is written.
        read_back_status = outcome.read_back_status
        if read_back_status == READ_BACK_SEND_ACK:
            # OBS-20260919T0055Z_PROBE_CALL_OUTSIDE_TRY: the probe is an
            # optional, best-effort read-back check. It used to be called
            # unguarded, so a probe-capable adapter whose probe raised would let
            # that exception escape dispatch_one() — and the delivery_worker
            # service loop (src/antigona/delivery_worker.py) wraps dispatch_one()
            # in no guard at all, so the loop would die. The send itself is
            # already durable (item.delivered_at was committed above), so a probe
            # failure is *not* evidence about the message: it is exactly the same
            # "cannot probe" case as probe=None. Fail-closed with respect to the
            # claim: on any probe error the stored SEND_ACK is left exactly as it
            # is — never downgraded to REFUTED, never upgraded to anything — and
            # processing of the item continues normally.
            try:
                probe = self._resolve_probe(channel_name)
                if probe is not None:
                    reconciled = reconcile_read_back(
                        outcome.provider_message_id, read_back_status, probe
                    )
                    if reconciled is not None:
                        read_back_status = reconciled
            except Exception:
                # A probe (or probe resolution) that raised gave no answer; leave
                # the asserted SEND_ACK untouched rather than inventing a
                # REFUTED, and never let it escape to kill the service loop.
                pass

        session.add(
            DeliveryReceipt(
                idempotency_key=item.idempotency_key,
                adapter=channel_name,
                task_id=item.task_id,
                delivered_at=now,
                transmitted=transmitted,
                provider_message_id=outcome.provider_message_id,
                read_back_status=read_back_status,
                read_back_at=now if read_back_status == READ_BACK_SEND_ACK else None,
            )
        )
        if transmitted:
            item.status = "DELIVERED"
            item.delivered_at = now
        else:
            # Simulated / mock dispatch: nothing left the process. It must reach a
            # distinct terminal status so it can never be read as a real send.
            item.status = "SIMULATED"
            item.delivered_at = None
        item.lease_owner = None
        item.lease_expires_at = None
        session.commit()
        return True
