from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from antigona.database import Database
from antigona.repository import CreateTask, TaskRepository


def test_concurrent_idempotent_create_returns_one_task(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'race.db'}")
    database.create_all()
    command = CreateTask("owner", "goal", "file", "content", "race-key")

    def create() -> str:
        with database.session_factory() as session:
            return TaskRepository(session).create(command)[0].id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create(), range(2)))
    assert len(set(ids)) == 1
