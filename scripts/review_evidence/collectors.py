"""Read-only collectors and critical detectors for the Review & Evidence Pipeline."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow importing from src/ when running as a script without install.
_REPO_CANDIDATE = Path(__file__).resolve().parents[2]
_SRC = _REPO_CANDIDATE / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@dataclass
class Finding:
    id: str
    severity: str  # critical|high|medium|low|info
    subsystem: str
    title: str
    detail: str
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class CollectorResult:
    subsystem: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    raw_text: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cmd(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else f"timeout after {timeout}s"
        return 124, out, err
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def collect_git(repo: Path) -> CollectorResult:
    lines: list[str] = []
    data: dict[str, Any] = {}
    findings: list[Finding] = []

    def g(*a: str) -> str:
        code, out, err = run_cmd(["git", *a], cwd=repo, timeout=60)
        text = out if out else err
        lines.append(f"$ git {' '.join(a)}\n{text}".rstrip())
        return out.strip()

    head = g("rev-parse", "HEAD")
    branch = g("branch", "--show-current")
    status = g("status", "-sb")
    porcelain = g("status", "--porcelain")
    remotes = g("remote", "-v")
    branches = g("branch", "-vv")
    worktrees = g("worktree", "list")
    stash = g("stash", "list")
    log = g("log", "--oneline", "-20")

    data.update(
        {
            "head": head,
            "branch": branch,
            "status_sb": status,
            "remotes": remotes,
            "worktrees": worktrees.splitlines(),
            "stash": stash.splitlines(),
            "log_20": log.splitlines(),
            "branches": branches,
        }
    )

    if not head or len(head) < 7:
        findings.append(
            Finding(
                id="GIT_HEAD_MISSING",
                severity="critical",
                subsystem="git",
                title="Cannot resolve git HEAD",
                detail="git rev-parse HEAD failed or empty",
                evidence="RAW/git_snapshot.txt",
            )
        )

    dirty_tracked = [
        ln for ln in porcelain.splitlines() if ln and not ln.startswith("??")
    ]
    data["dirty_tracked"] = dirty_tracked
    data["untracked"] = [ln for ln in porcelain.splitlines() if ln.startswith("??")]
    if dirty_tracked:
        findings.append(
            Finding(
                id="GIT_DIRTY_TRACKED",
                severity="medium",
                subsystem="git",
                title="Tracked files are dirty",
                detail=f"{len(dirty_tracked)} dirty tracked path(s)",
                evidence="RAW/git_snapshot.txt",
            )
        )

    return CollectorResult(
        subsystem="git",
        ok=not any(f.severity == "critical" for f in findings),
        summary=f"branch={branch or '?'} head={(head or '?')[:12]} dirty_tracked={len(dirty_tracked)}",
        data=data,
        findings=findings,
        raw_text="\n\n".join(lines) + "\n",
    )


def collect_runtime(repo: Path, *, require_live: bool) -> CollectorResult:
    lines: list[str] = []
    data: dict[str, Any] = {}
    findings: list[Finding] = []

    # Process snapshot (best-effort, Linux)
    code, out, err = run_cmd(
        ["bash", "-lc", "ps aux | rg -i 'antigona|gateway|worker|verifier|delivery|telegram' || true"],
        cwd=repo,
        timeout=30,
    )
    lines.append(f"$ ps (filtered)\n{out or err}")
    data["ps_filtered"] = out

    code, out, err = run_cmd(
        ["bash", "-lc", "ss -tlnp 2>/dev/null | head -60 || true"],
        cwd=repo,
        timeout=15,
    )
    lines.append(f"$ ss -tlnp\n{out or err}")
    data["ports"] = out

    # Gateway health
    health_ok = False
    health_body = ""
    for port in (8090, 8000, 8080):
        try:
            import urllib.request

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                health_body = resp.read().decode("utf-8", errors="replace")
                health_ok = resp.status == 200
                data["gateway_port"] = port
                data["gateway_health"] = health_body
                lines.append(f"$ GET :{port}/health\n{health_body}")
                break
        except Exception as exc:  # noqa: BLE001 — inventory
            lines.append(f"$ GET :{port}/health failed: {exc}")
    data["gateway_health_ok"] = health_ok

    if require_live and not health_ok:
        findings.append(
            Finding(
                id="GATEWAY_HEALTH_DOWN",
                severity="critical",
                subsystem="runtime",
                title="Gateway health check failed",
                detail="--require-live set but no healthy /health on common ports",
                evidence="RAW/runtime_snapshot.txt",
            )
        )
    elif not health_ok:
        findings.append(
            Finding(
                id="GATEWAY_HEALTH_ABSENT",
                severity="info",
                subsystem="runtime",
                title="Gateway not responding (optional in this mode)",
                detail="No /health on 8090/8000/8080",
                evidence="RAW/runtime_snapshot.txt",
            )
        )

    # PYTHONPATH contamination for known PIDs (best-effort)
    contamination: list[str] = []
    for line in (data.get("ps_filtered") or "").splitlines():
        m = re.search(r"^\S+\s+(\d+)\s+", line)
        if not m:
            continue
        pid = m.group(1)
        env_path = Path(f"/proc/{pid}/environ")
        if not env_path.is_file():
            continue
        try:
            env = env_path.read_bytes().split(b"\0")
            for item in env:
                if item.startswith(b"PYTHONPATH=") and b"hermes" in item.lower():
                    contamination.append(f"pid={pid} {item.decode('utf-8', errors='replace')}")
        except OSError:
            continue
    data["pythonpath_hermes"] = contamination
    if contamination:
        findings.append(
            Finding(
                id="RUNTIME_PYTHONPATH_HERMES",
                severity="high",
                subsystem="runtime",
                title="Antigona process has Hermes on PYTHONPATH",
                detail=f"{len(contamination)} process env hit(s)",
                evidence="RAW/runtime_snapshot.txt",
            )
        )

    return CollectorResult(
        subsystem="runtime",
        ok=not any(f.severity == "critical" for f in findings),
        summary=f"gateway_health_ok={health_ok} hermes_pythonpath={len(contamination)}",
        data=data,
        findings=findings,
        raw_text="\n".join(lines) + "\n",
    )


def detect_sessions_last_n(repo: Path) -> CollectorResult:
    """Reproduce P-01: get_messages(limit=N) must return newest N in chrono order."""
    findings: list[Finding] = []
    data: dict[str, Any] = {}
    try:
        from antigona.sessions.database import SessionDatabase  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return CollectorResult(
            subsystem="sessions",
            ok=False,
            summary=f"import failed: {exc}",
            findings=[
                Finding(
                    id="SESSIONS_IMPORT_FAIL",
                    severity="high",
                    subsystem="sessions",
                    title="Cannot import SessionDatabase",
                    detail=str(exc),
                )
            ],
        )

    async def _run() -> dict[str, Any]:
        db = SessionDatabase(db_path=":memory:")
        await db.connect()
        await db.create_session("review-evidence-s1")
        for i in range(15):
            await db.add_message("review-evidence-s1", "user", f"msg-{i}")
        msgs = await db.get_messages("review-evidence-s1", limit=5)
        contents = [m["content"] for m in msgs]
        await db.close()
        oldest = contents == [f"msg-{i}" for i in range(5)]
        newest = contents == [f"msg-{i}" for i in range(10, 15)]
        return {
            "contents": contents,
            "returns_oldest_five": oldest,
            "returns_newest_five_chrono": newest,
        }

    try:
        data = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return CollectorResult(
            subsystem="sessions",
            ok=False,
            summary=f"repro crashed: {exc}",
            findings=[
                Finding(
                    id="SESSIONS_REPRO_CRASH",
                    severity="high",
                    subsystem="sessions",
                    title="sessions last-N repro crashed",
                    detail=str(exc),
                )
            ],
        )

    if data.get("returns_oldest_five"):
        findings.append(
            Finding(
                id="S_P01_OLDEST_N",
                severity="critical",
                subsystem="sessions",
                title="get_messages returns oldest N (P-01 defect)",
                detail=(
                    "limit=5 on 15 messages returned first messages, not last five. "
                    f"got={data.get('contents')}"
                ),
                evidence="RAW/subsystem_sessions.json",
            )
        )
    elif not data.get("returns_newest_five_chrono"):
        findings.append(
            Finding(
                id="S_P01_UNEXPECTED_ORDER",
                severity="high",
                subsystem="sessions",
                title="get_messages order unexpected",
                detail=f"got={data.get('contents')}",
                evidence="RAW/subsystem_sessions.json",
            )
        )

    ok = not any(f.severity in {"critical", "high"} for f in findings)
    return CollectorResult(
        subsystem="sessions",
        ok=ok,
        summary=(
            f"oldest={data.get('returns_oldest_five')} "
            f"newest={data.get('returns_newest_five_chrono')}"
        ),
        data=data,
        findings=findings,
        raw_text=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def detect_policy_fail_open(repo: Path) -> CollectorResult:
    findings: list[Finding] = []
    data: dict[str, Any] = {}
    try:
        from antigona.tools.registry import ToolRegistry  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return CollectorResult(
            subsystem="tools",
            ok=False,
            summary=f"import failed: {exc}",
            findings=[
                Finding(
                    id="TOOLS_IMPORT_FAIL",
                    severity="high",
                    subsystem="tools",
                    title="Cannot import ToolRegistry",
                    detail=str(exc),
                )
            ],
        )

    async def _run() -> dict[str, Any]:
        reg = ToolRegistry()

        async def handler(**_kw: Any) -> str:
            return json.dumps({"ok": True, "executed": True})

        reg.register("review_evidence_probe", toolset="test", schema={}, handler=handler)

        class Boom:
            async def check(self, *a: Any, **k: Any) -> dict[str, Any]:
                raise RuntimeError("policy exploded (review-evidence detector)")

        reg.policy_engine = Boom()  # type: ignore[attr-defined]
        out = await reg.dispatch("review_evidence_probe", foo=1)
        parsed = json.loads(out)
        executed = parsed.get("executed") is True or parsed.get("ok") is True
        denied = "error" in parsed and not executed
        return {"raw": out, "parsed": parsed, "executed": executed, "denied": denied}

    try:
        data = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return CollectorResult(
            subsystem="tools",
            ok=False,
            summary=f"repro crashed: {exc}",
            findings=[
                Finding(
                    id="POLICY_REPRO_CRASH",
                    severity="high",
                    subsystem="tools",
                    title="policy fail-open repro crashed",
                    detail=str(exc),
                )
            ],
        )

    if data.get("executed"):
        findings.append(
            Finding(
                id="S1_POLICY_FAIL_OPEN",
                severity="critical",
                subsystem="tools",
                title="Policy exception fail-open (tool still executes)",
                detail=f"dispatch result={data.get('raw')}",
                evidence="RAW/subsystem_tools.json",
            )
        )

    ok = not any(f.severity == "critical" for f in findings)
    return CollectorResult(
        subsystem="tools",
        ok=ok,
        summary=f"executed_despite_policy_exception={data.get('executed')} denied={data.get('denied')}",
        data=data,
        findings=findings,
        raw_text=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def detect_approval_grant(repo: Path) -> CollectorResult:
    findings: list[Finding] = []
    path = repo / "src" / "antigona" / "security" / "approval_grant.py"
    present = path.is_file()
    data = {"path": str(path), "present": present}
    if not present:
        findings.append(
            Finding(
                id="APPROVAL_GRANT_MISSING",
                severity="critical",
                subsystem="approvals",
                title="ApprovalGrant module missing on tree",
                detail=f"expected file absent: {path}",
                evidence="RAW/subsystem_approvals.json",
            )
        )
    else:
        # Light static check: class name present
        text = path.read_text(encoding="utf-8", errors="replace")
        data["has_class"] = "class ApprovalGrant" in text or "ApprovalGrant" in text
        if not data["has_class"]:
            findings.append(
                Finding(
                    id="APPROVAL_GRANT_EMPTY",
                    severity="high",
                    subsystem="approvals",
                    title="approval_grant.py present but ApprovalGrant symbol not found",
                    detail="file exists without expected symbol",
                    evidence="RAW/subsystem_approvals.json",
                )
            )

    return CollectorResult(
        subsystem="approvals",
        ok=present and not findings,
        summary=f"approval_grant.py present={present}",
        data=data,
        findings=findings,
        raw_text=json.dumps(data, indent=2) + "\n",
    )


def collect_parity(repo: Path) -> CollectorResult:
    findings: list[Finding] = []
    data: dict[str, Any] = {}
    try:
        from antigona.core.command_registry import (  # type: ignore
            command_registry,
            commands_for_channel,
        )

        regs = command_registry()
        cli = {getattr(c, "name", None) for c in commands_for_channel("cli")}
        tg = {getattr(c, "name", None) for c in commands_for_channel("telegram")}
        cli.discard(None)
        tg.discard(None)
        data = {
            "total_specs": len(regs),
            "cli": sorted(cli),
            "telegram": sorted(tg),
            "cli_only": sorted(cli - tg),
            "tg_only": sorted(tg - cli),
            "both": sorted(cli & tg),
        }
        # Surface-specific exit/reset is OK; flag large unexplained drift
        unexpected_cli = set(data["cli_only"]) - {"exit"}
        unexpected_tg = set(data["tg_only"]) - {"reset"}
        if unexpected_cli or unexpected_tg:
            findings.append(
                Finding(
                    id="PARITY_DRIFT",
                    severity="medium",
                    subsystem="parity",
                    title="CLI/Telegram command set drift beyond exit/reset",
                    detail=f"cli_only_extra={sorted(unexpected_cli)} tg_only_extra={sorted(unexpected_tg)}",
                    evidence="RAW/subsystem_parity.json",
                )
            )
        summary = (
            f"specs={len(regs)} both={len(data['both'])} "
            f"cli_only={data['cli_only']} tg_only={data['tg_only']}"
        )
        ok = True
    except Exception as exc:  # noqa: BLE001
        summary = f"import/parity failed: {exc}"
        ok = False
        findings.append(
            Finding(
                id="PARITY_IMPORT_FAIL",
                severity="high",
                subsystem="parity",
                title="Cannot build command parity matrix",
                detail=str(exc),
            )
        )

    return CollectorResult(
        subsystem="parity",
        ok=ok,
        summary=summary,
        data=data,
        findings=findings,
        raw_text=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def collect_portrait_heuristic(repo: Path) -> CollectorResult:
    """Heuristic: portrait modules should not open DB/SQLAlchemy engines."""
    findings: list[Finding] = []
    root = repo / "src" / "antigona" / "cli_ui"
    files = sorted(root.glob("portrait*.py"))
    hits: list[str] = []
    banned = re.compile(
        r"\b(create_engine|sqlalchemy|psycopg|SessionLocal|antigona\.db)\b"
    )
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if banned.search(line) and not line.strip().startswith("#"):
                hits.append(f"{f.relative_to(repo)}:{i}:{line.strip()[:120]}")
    data = {"portrait_files": [str(f.relative_to(repo)) for f in files], "db_hits": hits}
    if hits:
        findings.append(
            Finding(
                id="PORTRAIT_BACKEND_HINT",
                severity="high",
                subsystem="portrait",
                title="Portrait modules reference DB/engine symbols",
                detail="; ".join(hits[:5]),
                evidence="RAW/subsystem_portrait.json",
            )
        )
    present = bool(files)
    if not present:
        findings.append(
            Finding(
                id="PORTRAIT_MISSING",
                severity="medium",
                subsystem="portrait",
                title="No portrait*.py modules found",
                detail=str(root),
            )
        )
    return CollectorResult(
        subsystem="portrait",
        ok=not any(f.severity in {"critical", "high"} for f in findings),
        summary=f"files={len(files)} db_hits={len(hits)}",
        data=data,
        findings=findings,
        raw_text=json.dumps(data, indent=2) + "\n",
    )


def collect_providers(repo: Path) -> CollectorResult:
    root = repo / "src" / "antigona" / "providers"
    files = sorted(p.name for p in root.glob("*.py") if p.name != "__init__.py") if root.is_dir() else []
    data: dict[str, Any] = {"files": files}
    findings: list[Finding] = []
    try:
        from antigona.providers import profiles  # type: ignore

        reg = getattr(profiles, "ProfileRegistry", None) or getattr(profiles, "PROFILES", None)
        data["profiles_attr"] = type(reg).__name__ if reg is not None else None
        # Enumerate known profile constants if present
        names = [n for n in dir(profiles) if n.isupper() or n.endswith("_PROFILE")]
        data["profile_symbols"] = names[:40]
    except Exception as exc:  # noqa: BLE001
        findings.append(
            Finding(
                id="PROVIDERS_IMPORT",
                severity="medium",
                subsystem="providers",
                title="providers import issue",
                detail=str(exc),
            )
        )
    return CollectorResult(
        subsystem="providers",
        ok=bool(files),
        summary=f"provider_modules={len(files)}",
        data=data,
        findings=findings,
        raw_text=json.dumps(data, indent=2) + "\n",
    )


def collect_docs_ssot(repo: Path) -> CollectorResult:
    checks = {
        "PROJECT_CHECKPOINT.md": (repo / "PROJECT_CHECKPOINT.md").is_file(),
        "docs/ROADMAP.md": (repo / "docs" / "ROADMAP.md").is_file(),
        "docs/ARCHITECTURE.md": (repo / "docs" / "ARCHITECTURE.md").is_file(),
        "README.md": (
            repo / "docs" / "review-evidence-pipeline" / "README.md"
        ).is_file(),
        "SECURITY_CONTRACT.md": (repo / "SECURITY_CONTRACT.md").is_file()
        or (repo / "docs" / "SECURITY_CONTRACT.md").is_file(),
    }
    findings: list[Finding] = []
    if not checks["PROJECT_CHECKPOINT.md"]:
        findings.append(
            Finding(
                id="DOC_NO_CHECKPOINT",
                severity="medium",
                subsystem="docs_ssot",
                title="PROJECT_CHECKPOINT.md missing",
                detail="No single project checkpoint file at repo root",
            )
        )
    if not checks.get("SECURITY_CONTRACT.md"):
        findings.append(
            Finding(
                id="DOC_NO_SECURITY_CONTRACT",
                severity="low",
                subsystem="docs_ssot",
                title="SECURITY_CONTRACT.md missing",
                detail="Optional but recommended SSOT for security contract",
            )
        )
    return CollectorResult(
        subsystem="docs_ssot",
        ok=True,
        summary="presence=" + json.dumps(checks),
        data=checks,
        findings=findings,
        raw_text=json.dumps(checks, indent=2) + "\n",
    )


def collect_path_inventory(repo: Path, subsystem: str, paths: list[str]) -> CollectorResult:
    existing: list[str] = []
    missing: list[str] = []
    for p in paths:
        target = repo / p
        if target.exists():
            existing.append(p)
        else:
            missing.append(p)
    findings: list[Finding] = []
    if missing and subsystem not in {"docs_ssot"}:
        findings.append(
            Finding(
                id=f"PATHS_MISSING_{subsystem.upper()}",
                severity="low",
                subsystem=subsystem,
                title=f"Some paths missing for {subsystem}",
                detail=", ".join(missing[:10]),
            )
        )
    return CollectorResult(
        subsystem=subsystem,
        ok=True,
        summary=f"existing={len(existing)} missing={len(missing)}",
        data={"existing": existing, "missing": missing},
        findings=findings,
        raw_text=json.dumps({"existing": existing, "missing": missing}, indent=2) + "\n",
    )


def parse_pytest_summary(text: str) -> dict[str, int]:
    """Parse pytest short summary line."""
    result = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "xfailed": 0}
    # e.g. "2 failed, 3204 passed, 3 skipped, 3 warnings in 199.56s"
    for key in result:
        m = re.search(rf"(\d+)\s+{key}", text)
        if m:
            result[key] = int(m.group(1))
    return result


def run_pytest(
    repo: Path,
    targets: list[str],
    *,
    out_file: Path,
    timeout: int = 600,
) -> dict[str, Any]:
    existing = [t for t in targets if (repo / t).exists() or any(Path(repo).glob(t))]
    # glob expansion
    expanded: list[str] = []
    for t in targets:
        p = repo / t
        if p.exists():
            expanded.append(t)
            continue
        matches = list(repo.glob(t))
        if matches:
            expanded.extend(str(m.relative_to(repo)) for m in matches)
    # unique preserve order
    seen: set[str] = set()
    final: list[str] = []
    for t in expanded:
        if t not in seen:
            seen.add(t)
            final.append(t)

    if not final:
        out_file.write_text("NO_TARGETS\n", encoding="utf-8")
        return {"ran": False, "reason": "no targets", "passed": 0, "failed": 0, "skipped": 0}

    venv_py = repo / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.is_file() else sys.executable
    cmd = [py, "-m", "pytest", "-q", "--tb=line", *final]
    code, out, err = run_cmd(cmd, cwd=repo, timeout=timeout)
    text = out + ("\n" + err if err else "")
    out_file.write_text(text, encoding="utf-8")
    stats = parse_pytest_summary(text)
    stats.update({"ran": True, "exit_code": code, "cmd": cmd, "targets": final})
    return stats


def load_subsystems_yaml(repo: Path) -> dict[str, Any]:
    path = repo / "docs" / "review-evidence-pipeline" / "SUBSYSTEMS.yaml"
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except Exception:
        # Minimal fallback parser is not worth it; require PyYAML or json twin
        # Try JSON twin if present
        jpath = path.with_suffix(".json")
        if jpath.is_file():
            return json.loads(jpath.read_text(encoding="utf-8"))
        # Ultra-light: use hardcoded modes if yaml unavailable
        raise RuntimeError(
            f"Cannot load {path}: install PyYAML or provide SUBSYSTEMS.json"
        ) from None
