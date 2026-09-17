"""Antigona clean-room P3 agent harness."""

from .workspace import (
    BaseWorkspace,
    DaytonaWorkspace,
    DockerWorkspace,
    LocalWorkspace,
    ModalWorkspace,
    SSHWorkspace,
    WorkspaceConnectionError,
    WorkspaceFactory,
)

__version__ = "1.0.0"

# ── Immutable code root guard ────────────────────────────────────────────────
# The deployed code root must never accumulate __pycache__ bytecode written at
# runtime: a stray write makes the tree drift from its sealed manifest.  Every
# console-script entry point imports this package before it loads its own
# module, so setting the flag here covers all of them.  Existing caches are
# still READ — only new writes are refused.  ``sitecustomize.py`` at the code
# root covers the ``python -m ...`` case (loaded by ``site`` at startup).
import sys as _sys

_sys.dont_write_bytecode = True

__all__ = [
    "BaseWorkspace",
    "LocalWorkspace",
    "DockerWorkspace",
    "SSHWorkspace",
    "ModalWorkspace",
    "DaytonaWorkspace",
    "WorkspaceConnectionError",
    "WorkspaceFactory",
]
