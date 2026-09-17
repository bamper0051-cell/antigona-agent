from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def request(method:str,url:str,body:dict[str,object]|None=None,headers:dict[str,str]|None=None)->dict[str,object]:
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url,data=data,method=method,headers={"Content-Type":"application/json",**(headers or {})})
    with urllib.request.urlopen(req,timeout=20) as response: return dict(json.load(response))


def wait_health(url:str)->None:
    for _ in range(100):
        try: request("GET",url); return
        except Exception: time.sleep(.1)
    raise RuntimeError(f"service unavailable: {url}")


def main()->None:
    with tempfile.TemporaryDirectory(prefix="antigona-e2e-") as root:
        def free_port()->int:
            with socket.socket() as sock: sock.bind(("127.0.0.1",0)); return int(sock.getsockname()[1])
        gateway_port,verifier_port=free_port(),free_port()
        base=Path(root); db=base/"antigona.db"; workspace=base/"workspace"
        credential=base/"verifier.credential"; credential.write_text("verifier-only-secret\n"); credential.chmod(0o600)
        common={key:value for key,value in os.environ.items() if key not in {"ANTIGONA_VERIFIER_CREDENTIAL","ANTIGONA_VERIFIER_CREDENTIAL_FILE"}}
        env={**common,"ANTIGONA_DATABASE_URL":f"sqlite:///{db}","ANTIGONA_WORKSPACE":str(workspace),"ANTIGONA_DEV_TOKENS":"gateway-token:alice","ANTIGONA_VERIFIER_URL":f"http://127.0.0.1:{verifier_port}","ANTIGONA_GATEWAY_PORT":str(gateway_port),"ANTIGONA_VERIFIER_PORT":str(verifier_port),"ANTIGONA_LEASE_SECONDS":"2","PYTHONUNBUFFERED":"1"}
        verifier_env={**env,"ANTIGONA_VERIFIER_CREDENTIAL_FILE":str(credential)}
        worker_env={**env,"ANTIGONA_VERIFIER_CREDENTIAL_FILE":str(credential)}
        gateway_env=dict(env)
        commands=[[sys.executable,"-m","antigona.verifier_service"],[sys.executable,"-m","antigona.gateway"],[sys.executable,"-m","antigona.worker"]]
        process_envs=[verifier_env,gateway_env,worker_env]
        processes=[subprocess.Popen(c,env=e,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) for c,e in zip(commands,process_envs,strict=True)]
        try:
            gateway=f"http://127.0.0.1:{gateway_port}"; verifier=f"http://127.0.0.1:{verifier_port}"
            wait_health(f"{gateway}/health")
            auth={"Authorization":"Bearer gateway-token","Idempotency-Key":"e2e-key"}
            task=request("POST",f"{gateway}/tasks",{"goal":"write proof","path":"proof.txt","content":"multiprocess docker proof"},auth); task_id=str(task["id"])
            waiting:dict[str,object]={}
            for _ in range(100):
                waiting=request("GET",f"{gateway}/tasks/{task_id}",headers={"Authorization":"Bearer gateway-token"})
                if waiting["status"]=="WAITING_APPROVAL": break
                time.sleep(.1)
            approval=dict(list(waiting["approvals"])[0])
            request("POST",f"{gateway}/tasks/{task_id}/approvals/{approval['id']}",{"approve":True},{"Authorization":"Bearer gateway-token"})
            final:dict[str,object]={}
            for _ in range(200):
                final=request("GET",f"{gateway}/tasks/{task_id}",headers={"Authorization":"Bearer gateway-token"})
                if final["status"] in {"DONE","FAILED","BLOCKED","TIMEOUT"}: break
                time.sleep(.1)
            try:
                request("POST",f"{verifier}/verify",{"task_id":task_id,"correlation_id":"forged"})
                negative="UNEXPECTED_SUCCESS"
            except urllib.error.HTTPError as exc: negative=f"HTTP_{exc.code}"
            artifact=dict(list(final["artifacts"])[0])
            print(f"gateway_pid={processes[1].pid} worker_pid={processes[2].pid} verifier_pid={processes[0].pid}")
            print(f"task={task_id} approval_gate={waiting['status']} final={final['status']}")
            print(f"artifact={artifact['path']} verified={artifact['verified']} sha256={artifact['sha256']}")
            print(f"runtime_negative_verifier_probe={negative}")
            print(f"gateway_has_verifier_credential={'ANTIGONA_VERIFIER_CREDENTIAL_FILE' in gateway_env or 'ANTIGONA_VERIFIER_CREDENTIAL' in gateway_env}")
            print(f"credential_mode={oct(credential.stat().st_mode & 0o777)}")
            print(f"workspace_mode={oct(workspace.stat().st_mode & 0o777)} content={(workspace/'proof.txt').read_text()}")
            if final["status"]!="DONE" or negative!="HTTP_401": raise RuntimeError("E2E invariant failed")

            shell=request("POST",f"{gateway}/tasks",{"goal":"shell proof","path":"shell.txt","content":"shell-flow","tool_name":"sandbox.shell","command":["sh","-c","printf shell-flow > shell.txt"]},{"Authorization":"Bearer gateway-token","Idempotency-Key":"shell-key"})
            shell_id=str(shell["id"])
            for _ in range(100):
                shell=request("GET",f"{gateway}/tasks/{shell_id}",headers={"Authorization":"Bearer gateway-token"})
                if shell["status"]=="WAITING_APPROVAL": break
                time.sleep(.1)
            shell_approval=dict(list(shell["approvals"])[0])
            request("POST",f"{gateway}/tasks/{shell_id}/approvals/{shell_approval['id']}",{"approve":True},{"Authorization":"Bearer gateway-token"})
            for _ in range(200):
                shell=request("GET",f"{gateway}/tasks/{shell_id}",headers={"Authorization":"Bearer gateway-token"})
                if shell["status"] in {"DONE","FAILED","BLOCKED","TIMEOUT"}: break
                time.sleep(.1)
            print(f"shell_task={shell_id} final={shell['status']} content={(workspace/'shell.txt').read_text()}")
            if shell["status"]!="DONE": raise RuntimeError("shell flow failed")

            cancel=request("POST",f"{gateway}/tasks",{"goal":"cancel shell","path":"never.txt","content":"never","tool_name":"sandbox.shell","command":["sh","-c","sleep 30; printf never > never.txt"]},{"Authorization":"Bearer gateway-token","Idempotency-Key":"cancel-key"})
            cancel_id=str(cancel["id"])
            for _ in range(100):
                cancel=request("GET",f"{gateway}/tasks/{cancel_id}",headers={"Authorization":"Bearer gateway-token"})
                if cancel["status"]=="WAITING_APPROVAL": break
                time.sleep(.1)
            cancel_approval=dict(list(cancel["approvals"])[0])
            request("POST",f"{gateway}/tasks/{cancel_id}/approvals/{cancel_approval['id']}",{"approve":True},{"Authorization":"Bearer gateway-token"})
            for _ in range(100):
                cancel=request("GET",f"{gateway}/tasks/{cancel_id}",headers={"Authorization":"Bearer gateway-token"})
                if cancel["status"]=="TOOL_EXECUTING": break
                time.sleep(.05)
            cancel=request("POST",f"{gateway}/tasks/{cancel_id}/cancel",headers={"Authorization":"Bearer gateway-token"})
            containers=""
            for _ in range(100):
                containers=subprocess.run(["docker","ps","--filter","name=antigona-","-q"],capture_output=True,text=True,check=True).stdout.strip()
                if not containers: break
                time.sleep(.1)
            print(f"http_cancel_task={cancel_id} final={cancel['status']} container_residual={containers or 'none'}")
            if cancel["status"]!="CANCELLED" or containers: raise RuntimeError("HTTP cancellation failed")

            crash=request("POST",f"{gateway}/tasks",{"goal":"crash reclaim","path":"once.txt","content":"once","tool_name":"sandbox.shell","command":["sh","-c","sleep 3; printf once >> once.txt"]},{"Authorization":"Bearer gateway-token","Idempotency-Key":"crash-key"})
            crash_id=str(crash["id"])
            for _ in range(100):
                crash=request("GET",f"{gateway}/tasks/{crash_id}",headers={"Authorization":"Bearer gateway-token"})
                if crash["status"]=="WAITING_APPROVAL": break
                time.sleep(.1)
            crash_approval=dict(list(crash["approvals"])[0])
            request("POST",f"{gateway}/tasks/{crash_id}/approvals/{crash_approval['id']}",{"approve":True},{"Authorization":"Bearer gateway-token"})
            for _ in range(100):
                crash=request("GET",f"{gateway}/tasks/{crash_id}",headers={"Authorization":"Bearer gateway-token"})
                if crash["status"]=="TOOL_EXECUTING": break
                time.sleep(.05)
            crashed_pid=processes[2].pid; processes[2].kill(); processes[2].wait(timeout=5)
            processes[2]=subprocess.Popen(commands[2],env=worker_env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            for _ in range(200):
                crash=request("GET",f"{gateway}/tasks/{crash_id}",headers={"Authorization":"Bearer gateway-token"})
                if crash["status"] in {"DONE","FAILED","BLOCKED","TIMEOUT"}: break
                time.sleep(.1)
            once=(workspace/"once.txt").read_text() if (workspace/"once.txt").exists() else "missing"
            print(f"worker_crash_pid={crashed_pid} reclaim_pid={processes[2].pid} final={crash['status']} duplicate_effect={once!r}")
            if crash["status"]!="DONE" or once!="once": raise RuntimeError("crash/reclaim duplicate effect")
        finally:
            for process in processes: process.terminate()
            for process in processes:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill()
                output=process.stdout.read() if process.stdout else ""
                if process.returncode not in {0,-15}: print(output)


if __name__=="__main__": main()
