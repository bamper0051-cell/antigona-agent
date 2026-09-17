from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from antigona.database import Database


def test_database_pool_lifecycle(tmp_path: Path) -> None:
    """Deterministically prove that a file-backed Database opens and checks in

    at least one pooled connection after create_all/session use, and dispose()
    leaves zero checked-in connections.
    """
    db_file = tmp_path / "test_lifecycle.db"

    db = Database(f"sqlite:///{db_file}")

    assert isinstance(db.engine.pool, QueuePool)

    # Initially, the connection pool has no connections
    assert db.engine.pool.checkedout() == 0
    assert db.engine.pool.checkedin() == 0

    # Perform schema creation and query execution
    db.create_all()
    for session in db.session():
        session.execute(text("SELECT 1"))

    # The connection should be returned to the pool (checked in)
    assert db.engine.pool.checkedout() == 0
    assert db.engine.pool.checkedin() >= 1

    # Dispose the database
    db.dispose()

    # The pool should be closed, leaving zero connections
    assert db.engine.pool.checkedout() == 0
    assert db.engine.pool.checkedin() == 0
