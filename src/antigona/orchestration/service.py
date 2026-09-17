"""Autonomous Goal Orchestration (M2) — live service entry.

Runs the GoalEngine in the real Antigona stack (started by run.sh). Writes a
file heartbeat like the other non-HTTP services (.health/goal_engine).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from antigona.config import Settings
from antigona.database import Database
from antigona.health.heartbeat import HeartbeatReporter
from antigona.kernel import KernelStore
from antigona.orchestration.engine import GoalEngine
from antigona.orchestration.store import OrchestrationStore
from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("antigona.orchestration.service")


def main() -> int:
    settings = Settings.from_env()
    db = Database(settings.database_url)
    db.create_all()
    kernel = KernelStore(db.session_factory)
    orch = OrchestrationStore(db.session_factory)

    grant_db = settings.database_url.replace("sqlite:///", "") or "antigona.db"
    grants = ApprovalGrantStore(db_path=grant_db)
    policy = PolicyEngine(require_approval=True, grant_store=grants)

    heartbeat = HeartbeatReporter("goal_engine", interval_seconds=15)
    heartbeat.start()

    engine = GoalEngine(
        kernel,
        orch,
        policy_engine=policy,
        grant_store=grants,
        lease_seconds=60,
        poll_interval=float(os.environ.get("GOAL_ENGINE_POLL", "1.0")),
        wait_retry_seconds=int(os.environ.get("GOAL_ENGINE_WAIT_RETRY", "60")),
        heartbeat=heartbeat,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig(signum: int, _frame: object) -> None:  # pragma: no cover
        logger.info("signal %s received, stopping", signum)
        engine.stop()
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        loop.run_until_complete(engine.run_forever())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        heartbeat.stop()
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
