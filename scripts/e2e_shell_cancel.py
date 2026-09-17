import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from antigona.shell import DockerShellTool, ShellInput

with TemporaryDirectory() as directory:
    root=Path(directory); tool=DockerShellTool(root,timeout_seconds=20)
    result=tool.execute(ShellInput(("sh","-c","printf shell-proof > shell.txt")))
    print(f"shell_ok={result.ok} file={(root/'shell.txt').read_text()} mode={oct(root.stat().st_mode % 512)}")
    cancelled=[]
    thread=threading.Thread(target=lambda:cancelled.append(tool.execute(ShellInput(("sh","-c","sleep 30")))))
    thread.start(); time.sleep(1); tool.cancel(); thread.join(10)
    print(f"cancel_thread_alive={thread.is_alive()} cancel_status={cancelled[0].status if cancelled else 'none'}")
    if thread.is_alive(): raise RuntimeError("Docker subprocess was not interrupted")
    if not cancelled or cancelled[0].status != "cancelled": raise RuntimeError("cancellation status was not preserved")
