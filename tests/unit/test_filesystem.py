from pathlib import Path

from antigona.contracts import WriteFileInput
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool


def test_write_and_symlink_containment(tmp_path:Path)->None:
    workspace=tmp_path/"w"; outside=tmp_path/"outside"; outside.mkdir(); workspace.mkdir(); (workspace/"link").symlink_to(outside,target_is_directory=True)
    tool=WorkspaceFileTool(InProcessTestBackend(workspace,test_mode=True))
    assert not tool.execute(WriteFileInput(path="link/pwn",content="no")).ok
    assert not (outside/"pwn").exists()
    result=tool.execute(WriteFileInput(path="safe/x",content="ok")); assert result.ok

def test_inprocess_requires_explicit_test_setting(tmp_path:Path)->None:
    try: InProcessTestBackend(tmp_path,test_mode=False)
    except RuntimeError: pass
    else: raise AssertionError("unsafe backend enabled")
