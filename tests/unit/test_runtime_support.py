from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from antigona.observability import event, redact, timed
from antigona.shell import DockerShellTool, ShellInput
from antigona.verifier_client import VerifierClient


class Process:
    returncode=0
    def communicate(self,timeout:int)->tuple[bytes,bytes]: del timeout; return b"ok",b""
    def poll(self)->int: return 0
    def terminate(self)->None: pass


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX st_mode bits (0o600/0o640/0o750) not enforceable on Windows (Wave 4)')
def test_shell_policy_output_permissions_and_quota(tmp_path:Path,monkeypatch:Any)->None:
    monkeypatch.setattr("subprocess.Popen",lambda *a,**k:Process())
    tool=DockerShellTool(tmp_path,output_cap=2,max_workspace_bytes=4)
    result=tool.execute(ShellInput(("printf","ok"))); assert result.ok and result.data["output"]=="ok"
    assert (tmp_path.stat().st_mode & 0o777)==0o750
    (tmp_path/"large").write_text("12345")
    assert tool.execute(ShellInput(("true",))).error=="workspace quota exceeded"


def test_structured_log_redaction(caplog:Any)->None:
    caplog.set_level(logging.INFO,logger="antigona")
    with timed("tool", service="worker", correlation_id="c", task_id="t", session_id="s", step_id="x", tool_name="shell"):
        pass
    event(
        "auth", service="gateway", correlation_id="c", task_id=None,
        session_id=None, step_id=None, authorization="Bearer secret-value", status="ok",
    )
    records=[json.loads(record.message) for record in caplog.records]
    assert records[0]["duration_ms"]>=0 and "secret-value" not in caplog.text
    assert redact({"password":"x"})=={"password":"[REDACTED]"}


def test_verifier_client_authenticated_json(monkeypatch:Any)->None:
    class Response:
        def __enter__(self)->Response: return self
        def __exit__(self,*args:object)->None: pass
        def read(self)->bytes: return b'{"decision":"DONE"}'
    seen:dict[str,Any]={}
    def open_(request:Any,timeout:int)->Response: seen["auth"]=request.headers["Authorization"]; seen["timeout"]=timeout; return Response()
    monkeypatch.setattr("urllib.request.urlopen",open_)
    assert VerifierClient("http://v","credential").request_verification("t","c")=="DONE"
    assert seen=={"auth":"Bearer credential","timeout":90}
