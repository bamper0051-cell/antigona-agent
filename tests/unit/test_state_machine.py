from pathlib import Path

import pytest

from antigona.database import Database
from antigona.models import TaskState
from antigona.repository import CreateTask, InvalidTransition, TaskRepository


def test_public_repository_cannot_forge_verifier(tmp_path:Path)->None:
    db=Database(f"sqlite:///{tmp_path/'db'}"); db.create_all()
    with db.session_factory() as session:
        repo=TaskRepository(session); task,_=repo.create(CreateTask("owner","g","x","y","key"))
        for target in (TaskState.QUEUED,TaskState.PLANNING,TaskState.TOOL_EXECUTING,TaskState.OBSERVING,TaskState.VERIFYING): repo.transition(task,target,"test","verifier-service")
        with pytest.raises(InvalidTransition,match="not exposed"): repo.transition(task,TaskState.DONE,"forged","verifier-service")
