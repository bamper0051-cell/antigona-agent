"""CLI entrypoint: permanent Review & Evidence Pipeline.

Delegates to ``scripts/review_evidence_pipeline.py`` so the pipeline stays a
repo-tooling concern (read-only evidence), not part of the runtime product path.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def _find_pipeline_script() -> Path:
    """Locate scripts/review_evidence_pipeline.py from install or source tree."""
    # 1) Source / editable: <repo>/src/antigona/this → <repo>/scripts/...
    here = Path(__file__).resolve()
    candidate = here.parents[2] / "scripts" / "review_evidence_pipeline.py"
    if candidate.is_file():
        return candidate

    # 2) Walk parents from cwd
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        p = parent / "scripts" / "review_evidence_pipeline.py"
        if p.is_file():
            return p

    raise FileNotFoundError(
        "Cannot find scripts/review_evidence_pipeline.py — run from Antigona repo root "
        "or use: python3 scripts/review_evidence_pipeline.py"
    )


def main(argv: list[str] | None = None) -> None:
    script = _find_pipeline_script()
    # Ensure scripts/ is importable for review_evidence package
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if argv is not None:
        sys.argv = [str(script), *argv]
    else:
        # keep user argv but replace argv0
        sys.argv = [str(script), *sys.argv[1:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
