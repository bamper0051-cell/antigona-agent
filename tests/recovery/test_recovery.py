from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.contracts import ToolResult, WriteFileInput
from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import TaskState
from antigona.orchestrator import Orchestrator
from antigona.repository import CreateTask, LeaseConflict, TaskRepository
from antigona.verifier_service import create_verifier_app


class CountingTool(WorkspaceFileTool):
    calls=0
    def execute(self,arguments:WriteFileInput)->ToolResult:
        self.calls+=1; return super().execute(arguments)


class VerifierHarness:
    def __init__(self,url:str,workspace:Path,monkeypatch:pytest.MonkeyPatch)->None:
        monkeypatch.setenv("ANTIGONA_WORKSPACE",str(workspace)); self.url=url; self.client=TestClient(create_verifier_app(url,"secret",deterministic_test_judge())); self.client.__enter__()
    def request_verification(self,task_id:str,correlation_id:str)->str:
        seed_private_criteria(self.url,task_id); response=self.client.post("/verify",headers={"Authorization":"Bearer secret"},json={"task_id":task_id,"correlation_id":correlation_id}); response.raise_for_status(); return str(response.json()["decision"])


def setup(tmp_path:Path)->tuple[Database,CountingTool,str]:
    db=Database(f"sqlite:///{tmp_path/'db'}"); db.create_all(); tool=CountingTool(InProcessTestBackend(tmp_path/"w",test_mode=True))
    with db.session_factory() as s:
        repo=TaskRepository(s); task,_=repo.create(CreateTask("o","g","a","body","k")); repo.request_approval(task).decision="APPROVED"; repo.commit(); return db,tool,task.id


@pytest.mark.parametrize("crash_state",[TaskState.RECEIVED,TaskState.QUEUED,TaskState.PLANNING,TaskState.TOOL_EXECUTING,TaskState.OBSERVING,TaskState.VERIFYING])
def test_recovery_all_nonterminal_checkpoints(tmp_path:Path,crash_state:TaskState,monkeypatch:pytest.MonkeyPatch)->None:
    db,tool,task_id=setup(tmp_path)
    with db.session_factory() as s:
        repo=TaskRepository(s); task=repo.get(task_id)
        for target in [TaskState.QUEUED,TaskState.PLANNING,TaskState.TOOL_EXECUTING,TaskState.OBSERVING,TaskState.VERIFYING]:
            if TaskState(task.status)==crash_state: break
            repo.transition(task,target,"crash fixture","test")
        repo.commit()
    verifier=VerifierHarness(db.engine.url.render_as_string(hide_password=False),tmp_path/"w",monkeypatch)
    with db.session_factory() as s: Orchestrator(s,tool,verifier).run(TaskRepository(s).get(task_id))
    with db.session_factory() as s: assert TaskRepository(s).get(task_id).status=="DONE"


def test_recovery_does_not_duplicate_observed_side_effect(tmp_path:Path,monkeypatch:pytest.MonkeyPatch)->None:
    db,tool,task_id=setup(tmp_path)
    with db.session_factory() as s:
        repo=TaskRepository(s); task=repo.get(task_id)
        for target in (TaskState.QUEUED,TaskState.PLANNING,TaskState.TOOL_EXECUTING): repo.transition(task,target,"fixture","test")
        tool.execute(WriteFileInput(path="a",content="body")); assert tool.calls==1; repo.commit()
    verifier=VerifierHarness(db.engine.url.render_as_string(hide_password=False),tmp_path/"w",monkeypatch)
    with db.session_factory() as s: Orchestrator(s,tool,verifier).run(TaskRepository(s).get(task_id))
    assert tool.calls==1


def test_single_writer_lease(tmp_path:Path)->None:
    db,_,task_id=setup(tmp_path)
    with db.session_factory() as a, db.session_factory() as b:
        ra,rb=TaskRepository(a),TaskRepository(b); ta,tb=ra.get(task_id),rb.get(task_id); ra.acquire_lease(ta,"one",60)
        with pytest.raises(LeaseConflict): rb.acquire_lease(tb,"two",60)
