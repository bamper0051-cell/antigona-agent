"""B19 regression: the HIGH_RISK_PATHS home entry is derived, not hardcoded.

Defect (B19): ``src/antigona/security/risk_classifier.py`` hardcoded the first
``HIGH_RISK_PATHS`` element as the literal ``"/opt/antigona-home"``, so off-host (HOME /
``ANTIGONA_HOME_DIR`` != ``/opt/antigona-home``) the classifier did not treat the real home
as high-risk. The home element MUST come from the project's single home
resolver ``antigona.core.paths.home_dir()`` (ADR-007 — never a second source of
truth for the home path). Every other element of the list is unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from antigona.core import paths
from antigona.security.risk_classifier import HIGH_RISK_PATHS

#: The system roots that must remain, in order, after the derived home element.
#: Kept verbatim so a reordered/dropped element fails loudly.
_SYSTEM_ROOTS_TAIL = [
    "/etc",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/var",
    "/boot",
    "/opt",
    "/lib",
    "/lib64",
    "/home",
    "/sys",
    "/proc",
    "/dev",
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "c:\\users\\public",
    "c:\\system volume information",
    "c:\\recovery",
]


def test_home_element_is_derived_from_home_dir() -> None:
    """The first element is the project's home resolver, never a literal."""
    assert HIGH_RISK_PATHS[0] == str(paths.home_dir())


def test_other_elements_unchanged_and_no_second_home_literal() -> None:
    """Only the home element changed; no hardcoded home remains elsewhere."""
    assert HIGH_RISK_PATHS[1:] == _SYSTEM_ROOTS_TAIL
    assert "/opt/antigona-home" not in HIGH_RISK_PATHS[1:]


def test_home_element_and_classification_follow_env_override(tmp_path: Path) -> None:
    """With ``ANTIGONA_HOME_DIR`` set the home element follows it (fresh import).

    Run in a subprocess so the module-level constant is imported under the
    overridden environment — the same way the affected tests are exercised.
    """
    fake_home = tmp_path / "fakehome_b19_xyz"
    fake_home.mkdir()
    probe = str(fake_home / "x")
    code = (
        "import json;"
        "from antigona.security.risk_classifier import HIGH_RISK_PATHS, RiskClassifier;"
        "print(json.dumps({"
        "'first': HIGH_RISK_PATHS[0],"
        "'tail': HIGH_RISK_PATHS[1:],"
        "'risk': str(RiskClassifier().classify('write_file', path=" + repr(probe) + ")),"
        "}))"
    )
    env = dict(os.environ)
    env["ANTIGONA_HOME_DIR"] = str(fake_home)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(paths.project_root()),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["first"] == str(fake_home)
    assert out["first"] != "/opt/antigona-home"
    assert out["tail"] == _SYSTEM_ROOTS_TAIL
    # The overridden home is treated as high-risk (was MEDIUM before the fix).
    assert out["risk"] == "HIGH"
