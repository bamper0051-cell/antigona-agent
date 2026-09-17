from __future__ import annotations

import os
import signal
import time

from .config import Settings
from .database import Database
from .delivery import DeliveryWorker, Router


def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    database.create_all()

    router = Router(settings)
    worker_id = os.getenv("ANTIGONA_DELIVERY_ID", "delivery")

    stopping = False

    def halt(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)

    # Scope 4 (health/readiness): liveness heartbeat for the Gateway's
    # aggregate /status (delivery has no HTTP port).
    from antigona.health.heartbeat import HeartbeatReporter

    delivery_heartbeat = HeartbeatReporter("delivery")
    delivery_heartbeat.start()

    while not stopping:
        delivery_heartbeat.stamp_progress()
        with database.session_factory() as session:
            worker = DeliveryWorker(session, worker_id=worker_id, router=router, settings=settings)
            if not worker.dispatch_one():
                time.sleep(0.1)


if __name__ == "__main__":
    main()