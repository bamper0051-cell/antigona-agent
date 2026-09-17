"""Offline-safe deployment diagnostics for the unified distribution."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.foundation import validate_foundation


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    required: bool = True
    detail: str = ""


@dataclass
class DoctorReport:
    root: Path
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    def summary(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "ok": self.ok,
            "passed": sum(check.ok for check in self.checks),
            "failed_required": [
                check.name for check in self.checks if check.required and not check.ok
            ],
            "missing_optional": [
                check.name for check in self.checks if not check.required and not check.ok
            ],
        }


def _module_check(import_name: str, *, required: bool) -> DoctorCheck:
    present = importlib.util.find_spec(import_name) is not None
    return DoctorCheck(
        name=f"dependency:{import_name}",
        ok=present,
        required=required,
        detail="installed" if present else "missing",
    )


def _env_mode_check(env_path: Path) -> DoctorCheck:
    if not env_path.exists():
        return DoctorCheck("env:file", False, detail=f"missing: {env_path}")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    safe = mode & 0o077 == 0
    return DoctorCheck(
        "env:permissions",
        safe,
        detail=f"mode={mode:04o}" + ("" if safe else " (expected 0600/0400)"),
    )


def _provider_checks() -> list[DoctorCheck]:
    """Provider configured via running env or active profile."""
    from antigona.providers.resolver import ProviderResolver

    info = ProviderResolver.get_active_info()
    if info.status != "active":
        return [DoctorCheck("providers:configuration", False, detail="no active LLM provider configured in env or secrets")]
    checks = [DoctorCheck("providers:primary", True, detail=f"provider={info.display_name}; model={info.model_name}; class={info.endpoint_class}")]
    return checks


def run_doctor(root: Path | None = None) -> DoctorReport:
    base = (root or paths.project_root()).resolve()
    report = DoctorReport(root=base)
    report.checks.append(
        DoctorCheck(
            "python>=3.11",
            sys.version_info >= (3, 11),
            detail=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    foundation = validate_foundation(base)
    report.checks.append(
        DoctorCheck(
            "foundation",
            foundation.ok,
            detail=(
                f"contracts={foundation.summary()['contracts']}; "
                f"datasets={foundation.summary()['datasets']}; "
                f"records={foundation.summary()['dataset_records']}"
            ),
        )
    )

    workspace = Path(os.getenv("ANTIGONA_WORKSPACE", str(paths.runtime_dir() / "workspace"))).resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        writable = os.access(workspace, os.W_OK)
    except OSError:
        writable = False
    report.checks.append(
        DoctorCheck("workspace:writable", writable, detail=str(workspace))
    )

    env_path = base / ".env"
    report.checks.append(_env_mode_check(env_path))

    for key in (
        "ANTIGONA_GATEWAY_TOKEN",
        "ANTIGONA_DEV_TOKENS",
        "ANTIGONA_VERIFIER_CREDENTIAL",
        "ANTIGONA_PIN",
    ):
        report.checks.append(
            DoctorCheck(
                f"security:{key}",
                bool(os.getenv(key)),
                detail="configured" if os.getenv(key) else "missing",
            )
        )

    report.checks.extend(_provider_checks())

    for module in (
        "fastapi",
        "sqlalchemy",
        "httpx",
        "rich",
        "prompt_toolkit",
        "typer",
        "aiosqlite",
        "croniter",
        "aiogram",
        "psycopg",
    ):
        report.checks.append(_module_check(module, required=True))
    report.checks.append(_module_check("ddgs", required=False))
    report.checks.append(_module_check("textual", required=False))
    return report
