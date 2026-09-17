"""Interpreter-startup bytecode guard for the Antigona code root.

``site`` imports this module at startup whenever the code root is on
``sys.path`` (e.g. a manual ``python -m antigona...`` run from the code root).
Setting ``sys.dont_write_bytecode`` here happens BEFORE the requested module is
compiled, so no ``__pycache__`` is written next to the immutable sources.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
