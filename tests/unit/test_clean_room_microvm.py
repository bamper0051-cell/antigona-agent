from __future__ import annotations

from pathlib import Path


def test_clean_room_no_forbidden_identifiers() -> None:
    forbidden = ["klio", "hermes", "openclaw"]
    target_files = [
        Path("src/antigona/sandbox/microvm.py"),
        Path("src/antigona/sandbox/__init__.py"),
        Path("src/antigona/worker/tools/shell_tool.py"),
    ]
    for path in target_files:
        assert path.exists(), f"File {path} does not exist"
        content = path.read_text(encoding="utf-8").lower()
        for term in forbidden:
            assert term not in content, f"Forbidden term {term!r} found in {path}"
