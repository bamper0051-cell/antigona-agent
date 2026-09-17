"""Clean-room audit test for cron implementation (P2.2 clean-room invariant)."""

from __future__ import annotations

import re
from pathlib import Path


def test_no_upstream_cron_imports() -> None:
    """Ensure src/ does not import upstream cron libraries."""
    src_dir = Path("src/antigona")
    forbidden_patterns = [
        re.compile(r"import\s+.*celery.*beat"),
        re.compile(r"from\s+.*celery.*beat\s+import"),
        re.compile(r"import\s+.*apscheduler"),
        re.compile(r"from\s+.*apscheduler\s+import"),
        re.compile(r"import\s+.*schedule\b"),
        re.compile(r"from\s+.*schedule\b\s+import"),
    ]

    for py_file in src_dir.glob("**/*.py"):
        content = py_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert not pattern.search(content), (
                f"{py_file} contains forbidden upstream cron import matching {pattern.pattern}"
            )


def test_no_delete_in_cron_module() -> None:
    """Ensure cron module does not contain DELETE statements (sticky cancel instead)."""
    cron_dir = Path("src/antigona/cron")
    files = list(cron_dir.glob("*.py"))
    if not files:
        cron_path = Path("src/antigona/cron.py")
        if cron_path.exists():
            files.append(cron_path)

    assert files, "No cron source files found to audit!"
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert "DELETE" not in content, f"{path.name} must not contain DELETE (use sticky cancel)"
