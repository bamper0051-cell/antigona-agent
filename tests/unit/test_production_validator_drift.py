"""Regression: the strict production validator must SEE code-root drift.

A hidden exclusion list is unacceptable: an exclusion may only remain if it is
provably required and asserted elsewhere.  This locks the narrow policy in and
proves a planted runtime file in the code root is reported as drift, not
silently swallowed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

#: The production validator lives in the deployment package, not in the repo.
_VALIDATOR_PATH = Path(
    os.environ.get(
        "ANTIGONA_PRODUCTION_VALIDATOR",
        "/var/lib/antigona-deployment/validate_production_deployment.py",
    )
)


def _load_validator():
    if not _VALIDATOR_PATH.is_file():
        pytest.skip(f"production validator not present at {_VALIDATOR_PATH}")
    spec = importlib.util.spec_from_file_location("antigona_production_validator", _VALIDATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(package_dir: Path, files: dict[str, str]) -> Path:
    mpath = package_dir / "PRODUCTION_DEPLOYMENT_MANIFEST.json"
    mpath.write_text(
        json.dumps(
            {
                "files": files,
                "file_count": len(files),
                "units": {},
                "runtime_mount": {"writers": [], "readers": []},
            }
        ),
        encoding="utf-8",
    )
    return mpath


def test_only_health_is_excluded() -> None:
    """The exclusion set is the minimum defensible one: ONLY `.health`."""
    validator = _load_validator()

    # Runtime artifact names must NOT be excluded -- they are drift in the code root.
    for name in (
        "elevation.db",
        "audit_log.db",
        "audit.db",
        "antigona.db",
        "antigona_sessions.db",
        "cli_state.json",
        "cli_aliases.json",
        "cli_theme.json",
        "cli_user_themes.json",
        "mcp_servers.json",
        "owner_pin.json",
        "traces.db",
        "traces.jsonl",
        "CANDIDATE_PROVENANCE.txt",
    ):
        assert validator.excluded(Path(name)) is False, f"{name} must not be excluded"
        assert validator.excluded(Path("sub") / name) is False, f"sub/{name} must not be excluded"
        assert validator.excluded(Path("ownership") / name) is False, f"ownership/{name} must not be excluded"
        assert validator.excluded(Path("phase_c1_evidence") / name) is False

    for name in ("pkg.pyc", "core.py", "nested/__pycache__/x.pyc"):
        assert validator.excluded(Path(name)) is False, f"{name} must not be excluded"

    assert validator.excluded(Path(".health")) is True
    assert validator.excluded(Path(".health/worker.json")) is True


def test_planted_runtime_file_in_code_root_is_detected(tmp_path: Path) -> None:
    """A runtime file planted in the code root FAILS validation (no exclusion)."""
    validator = _load_validator()

    code_root = tmp_path / "code_root"
    code_root.mkdir()
    (code_root / "core.py").write_text("x = 1\n", encoding="utf-8")
    (code_root / "elevation.db").write_bytes(b"SQLite format 3\x00planted")

    package_dir = tmp_path / "package"
    package_dir.mkdir()
    mpath = _manifest(package_dir, {"core.py": _sha(code_root / "core.py")})

    report = validator.evaluate(package_dir, code_root, mpath)
    assert report["verdict"] == "FAIL", report
    assert "elevation.db" in report["extra"], report
    assert report["mismatches"] >= 1


def test_clean_code_root_passes(tmp_path: Path) -> None:
    """With no planted runtime file the same tree is VALID (control)."""
    validator = _load_validator()

    code_root = tmp_path / "code_root"
    code_root.mkdir()
    (code_root / "core.py").write_text("x = 1\n", encoding="utf-8")

    package_dir = tmp_path / "package"
    package_dir.mkdir()
    mpath = _manifest(package_dir, {"core.py": _sha(code_root / "core.py")})

    report = validator.evaluate(package_dir, code_root, mpath)
    assert report["verdict"] == "PASS", report
    assert report["checked"] == 1
    assert report["extra"] == []


def test_dev_defaults_owner_stores_never_hidden_by_default() -> None:
    """Ownership/CLI/audit names are exactly the ones a hidden list would hide."""
    _load_validator()  # must be loadable
    hidden_terms = {"ownership", "audit_log.db", "elevation.db", "cli_state.json"}
    src = _VALIDATOR_PATH.read_text(encoding="utf-8")
    # The only literal in the exclusion policy is `.health`; the runtime names
    # above must appear nowhere near `excluded()`.
    policy = src.split("def h(x: Path)")[0]
    for term in hidden_terms:
        assert term not in policy, f"{term} leaked into the exclusion policy"
