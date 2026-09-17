"""Negative test: replay module must not import upstream tracer libraries.

This test verifies the clean-room constraint documented in P2_3_PLAN.md §5.1:
ReplayEngine is self-written, without importing upstream tracing libraries
such as celery.result, temporal, or prefect.
"""

from pathlib import Path


def _module_source(name: str) -> str:
    """Return the source text of a module under src/antigona/."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    path = repo_root / "src" / "antigona" / name
    return path.read_text(encoding="utf-8")


FORBIDDEN_IMPORTS = [
    "celery.result",
    "celery",
    "temporal",
    "prefect",
    "langsmith",
    "langfuse",
    "openhands.sdk",
    "mlflow",
    "phoenix",
]


def test_clean_room_replay_no_upstream_tracers() -> None:
    source = _module_source("replay.py")
    for banned in FORBIDDEN_IMPORTS:
        assert banned not in source, f"replay.py imports forbidden upstream library: {banned}"


def test_clean_room_api_no_tracer_imports() -> None:
    source = _module_source("gateway/api.py")
    for banned in FORBIDDEN_IMPORTS:
        assert banned not in source, f"gateway/api.py imports forbidden upstream library: {banned}"
