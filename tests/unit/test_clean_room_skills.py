"""Clean-room audit test for skills implementation (P2.1 clean-room invariant)."""

from __future__ import annotations

import re
from pathlib import Path


def test_no_yaml_frontmatter_in_src_skills() -> None:
    """Ensure no YAML-frontmatter Markdown skill files exist in src/ or skills package."""
    src_dir = Path("src/antigona")
    for file_path in src_dir.glob("**/*"):
        if file_path.is_file() and file_path.suffix in (".md", ".yaml", ".yml"):
            content = file_path.read_text(encoding="utf-8")
            assert not content.startswith("---\nname:"), f"{file_path} contains YAML-frontmatter skill header"


def test_no_third_party_skill_imports() -> None:
    """Ensure src/ does not import upstream skill packages."""
    src_dir = Path("src/antigona")
    forbidden_patterns = [
        re.compile(r"import\s+.*openhands.*skills"),
        re.compile(r"from\s+.*openhands.*skills\s+import"),
        re.compile(r"import\s+.*anthropic.*skills"),
        re.compile(r"from\s+.*anthropic.*skills\s+import"),
        re.compile(r"import\s+.*fastmcp.*skills"),
        re.compile(r"from\s+.*fastmcp.*skills\s+import"),
    ]

    for py_file in src_dir.glob("**/*.py"):
        content = py_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert not pattern.search(content), f"{py_file} contains forbidden upstream skill import matching {pattern.pattern}"
