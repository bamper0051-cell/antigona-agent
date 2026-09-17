"""Validation and discovery for the merged architectural foundation assets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antigona.core import paths

_REQUIRED_CONTRACTS = {
    "commands.json",
    "conversation_state.json",
    "event_contract.json",
    "intent_contract.json",
    "telegram_commands.json",
    "tool_contract.json",
}
_REQUIRED_DATASETS = {
    "adversarial_router_cases.jsonl",
    "conversation_acceptance_cases.jsonl",
    "intent_training_examples.jsonl",
}


@dataclass(frozen=True)
class AssetCheck:
    kind: str
    name: str
    ok: bool
    records: int = 0
    sha256: str = ""
    detail: str = ""


@dataclass
class FoundationReport:
    root: Path
    checks: list[AssetCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(item.ok for item in self.checks)

    def summary(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "ok": self.ok,
            "contracts": sum(c.kind == "contract" and c.ok for c in self.checks),
            "datasets": sum(c.kind == "dataset" and c.ok for c in self.checks),
            "dataset_records": sum(c.records for c in self.checks if c.kind == "dataset"),
            "prompts": sum(c.kind == "prompt" and c.ok for c in self.checks),
            "failures": [c.name for c in self.checks if not c.ok],
        }


def foundation_root(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    configured = paths.project_root()
    if (configured / "contracts").is_dir() and (configured / "datasets").is_dir():
        return configured
    # Editable/clean-clone fallback: <repo>/src/antigona/foundation.py -> <repo>.
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "contracts").is_dir() and (repo_root / "datasets").is_dir():
        return repo_root

    # Installed-wheel fallback. Foundation assets are duplicated inside the
    # package so validation still works when the source repository is absent.
    packaged = Path(__file__).resolve().parent / "resources" / "foundation"
    if (packaged / "contracts").is_dir() and (packaged / "datasets").is_dir():
        return packaged
    return configured


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_foundation(root: Path | None = None) -> FoundationReport:
    base = foundation_root(root)
    report = FoundationReport(root=base)

    for name in sorted(_REQUIRED_CONTRACTS):
        path = base / "contracts" / name
        if not path.exists():
            report.checks.append(AssetCheck("contract", name, False, detail="missing"))
            continue
        raw = path.read_bytes()
        try:
            json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            report.checks.append(
                AssetCheck("contract", name, False, sha256=_digest(raw), detail=str(exc))
            )
        else:
            report.checks.append(AssetCheck("contract", name, True, sha256=_digest(raw)))

    for name in sorted(_REQUIRED_DATASETS):
        path = base / "datasets" / name
        if not path.exists():
            report.checks.append(AssetCheck("dataset", name, False, detail="missing"))
            continue
        raw = path.read_bytes()
        records = 0
        bad_line = 0
        for lineno, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
                records += 1
            except json.JSONDecodeError:
                bad_line = lineno
                break
        report.checks.append(
            AssetCheck(
                "dataset",
                name,
                bad_line == 0,
                records=records,
                sha256=_digest(raw),
                detail=f"invalid JSONL at line {bad_line}" if bad_line else "",
            )
        )

    prompt_dir = base / "prompts"
    prompts = sorted(prompt_dir.glob("*.md")) if prompt_dir.is_dir() else []
    if not prompts:
        report.checks.append(AssetCheck("prompt", "*.md", False, detail="no prompts"))
    else:
        for path in prompts:
            raw = path.read_bytes()
            report.checks.append(
                AssetCheck("prompt", path.name, bool(raw.strip()), sha256=_digest(raw))
            )
    return report
