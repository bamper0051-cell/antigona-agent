from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from antigona.models import DeliveryOutbox, DeliveryReceipt, utcnow

from .adapter import DeliveryAdapter, ProgressEvent
from .errors import DeliveryPermanentError, sanitize_delivery_error
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
    ) -> None:
        self._factory: sessionmaker[Session] | None = (
            session if isinstance(session, sessionmaker) else None
        )
        self._session: Session | None = session if not isinstance(session, sessionmaker) else None
        self.adapter = adapter
        self.worker_id = worker_id
        self.router = router
        self.settings = settings

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
            # exists, so this never creates a duplicate.
            channel_name = item.adapter or "progress"
            session.add(
                DeliveryReceipt(
                    idempotency_key=item.idempotency_key,
                    adapter=channel_name,
                    task_id=item.task_id,
                    delivered_at=item.delivered_at,
                    transmitted=True,
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
        )
        channel_name = item.adapter or "progress"

        try:
            if self.router is not None:
                transmitted = self.router.deliver(channel_name, event, item.idempotency_key)
            elif self.adapter is not None:
                transmitted = self.adapter.deliver(event, item.idempotency_key)
            else:
                from antigona.config import Settings

                self.settings = self.settings or Settings.from_env()
                self.router = Router(self.settings)
                transmitted = self.router.deliver(channel_name, event, item.idempotency_key)
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
        if transmitted:
            # Durable "send confirmed" marker written BEFORE the receipt is
            # constructed/committed. If the process crashes in the window between
            # here and the receipt commit below, this survives on the outbox row
            # so a reclaiming worker sees the send already happened and does not
            # re-transmit. The row stays SENDING (an expired lease is still
            # reclaimable), it just carries delivered_at now.
            item.delivered_at = now
            session.commit()
        session.add(
            DeliveryReceipt(
                idempotency_key=item.idempotency_key,
                adapter=channel_name,
                task_id=item.task_id,
                delivered_at=now,
                transmitted=transmitted,
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
