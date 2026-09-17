"""Runtime Validator — автоматическая проверка Runtime Contracts (ADR / Часть E).

На запуске (через run.sh, фазы pre/post) проверяет архитектурные инварианты:
- количество процессов каждого типа (C1-C5);
- открытые SQLite-базы (C6);
- источники Owner Identity / PIN / Bot Token (C7-C9);
- Runtime Provenance Chain — cwd и env-источник всех процессов (C10);
- процессы-сироты;
- конфликтующие конфигурации.

CRITICAL-нарушение -> НЕ запускать стек, детальный отчёт, exit != 0.
WARN -> логирование, запуск продолжается.

Запуск: ``python -m antigona.startup.validator --check=pre|post``
Аварийный обход: env ``ANTIGONA_SKIP_VALIDATOR=1``.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sqlite3
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from antigona.core import paths

_SEV_CRITICAL = "CRITICAL"
_SEV_WARN = "WARN"

# Служба -> маркер в cmdline (regex, границы токена).
SERVICE_MARKERS: dict[str, str] = {
    "gateway": r"antigona\.gateway(?![.\w])",
    "verifier": r"antigona\.verifier_service(?![.\w])",
    "worker": r"antigona\.worker(?![.\w])",
    "delivery_worker": r"antigona\.delivery_worker(?![.\w])",
    "telegram_bot": r"antigona\.channels\.telegram\.bot(?![.\w])",
}


@dataclass
class CheckResult:
    check: str
    severity: str
    ok: bool
    detail: str = ""
    #: Measured anchors behind the verdict (wave G1d, PLAN §1.3).  Last field on
    #: purpose: every existing construction site is positional, so this defaulted
    #: field is additive.  ``None`` means "no measured anchors" — a red verdict
    #: never fabricates them.
    evidence: dict[str, object] | None = None


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    start: float
    cmdline: str
    cwd: str


def _read_proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00")]
        return " ".join(parts).strip()
    except OSError:
        return ""


def _read_proc_cwd(pid: int) -> str:
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve())
    except OSError:
        return ""


def _proc_start(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/stat").read_text().splitlines():
            # comm may contain spaces in parens; stat time is after last ')'
            return float(line.rsplit(")", 1)[1].split()[19]) / 100.0  # starttime
    except Exception:
        return 0.0
    return 0.0


def _all_procs() -> list[ProcInfo]:
    out: list[ProcInfo] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = _read_proc_cmdline(pid)
        if not cmdline or "python" not in cmdline or "antigona" not in cmdline:
            continue
        try:
            stat = entry.joinpath("stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except Exception:
            ppid = 0
        out.append(ProcInfo(pid=pid, ppid=ppid, start=_proc_start(pid),
                            cmdline=cmdline, cwd=_read_proc_cwd(pid)))
    return out


def classify(procs: Iterable[ProcInfo]) -> dict[str, list[int]]:
    counts: dict[str, list[int]] = {k: [] for k in SERVICE_MARKERS}
    for p in procs:
        for svc, marker in SERVICE_MARKERS.items():
            if re.search(marker, p.cmdline):
                counts[svc].append(p.pid)
                break
    return counts


def open_db_files(procs: Iterable[ProcInfo]) -> set[str]:
    """Открытые Antigona-процессами .db-файлы (только наши, без docker/hermes)."""
    dbs: set[str] = set()
    antigona_pids = {p.pid for p in procs}
    for pid in antigona_pids:
        fd_dir = Path(f"/proc/{pid}/fd")
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = str(fd.resolve())
                except OSError:
                    continue
                if target.endswith(".db"):
                    dbs.add(target)
        except OSError:
            continue
    return dbs


def check_pre(procs: list[ProcInfo]) -> list[CheckResult]:
    """Pre-старт: не должно быть устаревших процессов стека (чистый старт).

    После pkill в run.sh здесь не должно остаться процессов служб (иначе —
    дубли/сироты в момент старта, сценарий «зависший бот»). Матчим только
    известные service-маркеры (не любой процесс, содержащий "antigona").
    """
    counts = classify(procs)
    stale = {svc: pids for svc, pids in counts.items() if pids}
    return [CheckResult(
        check="contract:pre:no_stale",
        severity=_SEV_CRITICAL,
        ok=not stale,
        detail="нет устаревших процессов стека" if not stale
               else f"обнаружены устаревшие процессы стека: {stale}",
    )]


def _count_checks(procs: list[ProcInfo]) -> list[CheckResult]:
    """C1-C5: каждая служба ровно 1 процесс (post-start)."""
    counts = classify(procs)
    results: list[CheckResult] = []
    for svc, pids in counts.items():
        results.append(CheckResult(
            check=f"contract:C1-5:{svc}",
            severity=_SEV_CRITICAL,
            ok=len(pids) == 1,
            detail=f"OK (PID {pids[0]})" if len(pids) == 1
                   else f"ожидается 1 процесс, найдено {len(pids)} -> {pids}",
        ))
    return results


def check_worktree_integrity() -> CheckResult:
    """Verify the release manifest using an explicit git-backed or gitless contract."""
    import hashlib
    import json
    import subprocess

    manifest_path = Path(os.environ.get("ANTIGONA_DEPLOYMENT_MANIFEST", "CANDIDATE_DEPLOYMENT_MANIFEST.json")).resolve()
    root = manifest_path.parent
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("schema") != "antigona-deployment-manifest/v2":
            raise ValueError("unsupported manifest schema")
        mode = data.get("mode")
        if mode not in {"git-backed", "gitless"}:
            raise ValueError("manifest mode must be git-backed or gitless")
        commit = data.get("commit")
        source_commit = data.get("source_commit")
        metadata_commit = data.get("release_metadata_commit")
        provenance = data.get("provenance")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("manifest commit invalid")
        if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise ValueError("manifest source_commit invalid")
        if not isinstance(metadata_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", metadata_commit):
            raise ValueError("manifest release_metadata_commit invalid")
        if not isinstance(provenance, dict) or provenance.get("source_commit") != source_commit:
            raise ValueError("manifest provenance does not name source_commit")
        if mode == "git-backed":
            actual_commit = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            if commit != actual_commit:
                raise ValueError(f"candidate commit mismatch: {commit!r} != {actual_commit!r}")

        files = data.get("files")
        if not isinstance(files, dict) or data.get("file_count") != len(files) or not files:
            raise ValueError("manifest file list/count invalid")
        report_rel = data.get("report")
        report_hash = data.get("report_sha256")
        if report_rel != "CANDIDATE_MANIFEST_HASH_REPORT.md" or not isinstance(report_hash, str):
            raise ValueError("manifest report contract missing")
        report_path = root / report_rel
        if report_path.is_symlink() or not report_path.is_file() or hashlib.sha256(report_path.read_bytes()).hexdigest() != report_hash:
            raise ValueError("manifest report tampered or missing")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        canonical = json.dumps({"schema": data["schema"], "mode": mode, "commit": commit,
                                "source_commit": source_commit, "release_metadata_commit": metadata_commit,
                                "provenance": provenance, "file_count": len(files), "scope": data["scope"], "files": files},
                               sort_keys=True, separators=(",", ":")).encode()
        if (report.get("manifest_sha256") != hashlib.sha256(canonical).hexdigest()
                or report.get("file_count") != len(files) or report.get("commit") != commit
                or report.get("source_commit") != source_commit
                or report.get("release_metadata_commit") != metadata_commit
                or report.get("mode") != mode):
            raise ValueError("manifest/report authentication mismatch")

        listed = set(files) | {report_rel, manifest_path.name}
        for rel, expected in files.items():
            relpath = Path(rel)
            if (not isinstance(rel, str) or relpath.is_absolute() or ".." in relpath.parts
                    or rel.startswith(("/", "\\")) or not re.fullmatch(r"[0-9a-f]{64}", str(expected))):
                raise ValueError(f"invalid manifest entry: {rel}")
            path = root / relpath
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError(f"manifest file is not an exclusive regular file: {rel}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"deployment file hash mismatch: {rel}")
        # Only these protected metadata files may exist outside the immutable scope.
        protected_metadata = {"AGENTS.md"}
        mutable_roots = {
            ".memory", ".tasks", ".task_messages", "downloads", "logs", "cache",
            "runtime", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache",
            ".ruff_cache", "evidence", "workspace", ".context", ".voice_cache",
            ".health", ".logs", "ownership",
        }
        mutable_files = {
            "antigona.db", "antigona_sessions.db", "antigona_memory.db", "audit.db",
            "audit_log.db", "cli_state.json", "cli_state.json.tmp", "elevation.db",
            "traces.db", "traces.jsonl", "mcp_servers.json", "owner_pin.json",
            "cli_aliases.json", "cli_theme.json", "cli_user_themes.json",
            "legacy_reachability.jsonl", ".voice_settings.json",
        }
        for path in root.rglob("*"):
            rel = path.relative_to(root).as_posix()
            if rel == ".git" or rel.startswith(".git/"):
                continue
            if rel in mutable_files or rel.split("/", 1)[0] in mutable_roots or "__pycache__" in rel.split("/") or any(part.endswith("_cache") for part in rel.split("/")):
                continue
            if path.is_symlink():
                raise ValueError(f"unknown symlink: {rel}")
            if path.is_dir():
                continue
            if path.stat().st_nlink != 1:
                raise ValueError(f"hardlink is not allowed: {rel}")
            if rel in listed or rel in protected_metadata:
                continue
            raise ValueError(f"unknown deployment file: {rel}")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        # No evidence dict on a refusal: the anchors above are only trustworthy once
        # the manifest and its report authenticated against each other, which by
        # definition did not happen here.
        return CheckResult("contract:C11:deployment_manifest", _SEV_CRITICAL, False, f"manifest verification failed: {exc}")
    return CheckResult("contract:C11:deployment_manifest", _SEV_CRITICAL, True,
                       f"immutable manifest verified ({mode}, source_commit {source_commit}, metadata identity {metadata_commit}, {len(files)} files)",
                       evidence={
                           "mode": mode,
                           "commit": commit,
                           "source_commit": source_commit,
                           "release_metadata_commit": metadata_commit,
                           "file_count": len(files),
                           "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
                           "report_sha256": report_hash,
                       })


# ── C11 verdict journal (wave G1d) ───────────────────────────────────────────

_C11_CHECK = "contract:C11:deployment_manifest"
#: The journal's own entry in the report. WARN by construction: writing evidence is
#: never allowed to gate the runtime (W4).
_EVIDENCE_CHAIN_CHECK = "contract:C11:evidence_chain"
_C11_JOURNAL_OPERATION = "c11/deployment-integrity"
_C11_JOURNAL_LINEAGE = "c11-deployment-integrity"
_C11_JOURNAL_ATTEMPTS = 3
_C11_DETAIL_LIMIT = 500


def _c11_journal_root() -> Path:
    """Chain root inside the runtime evidence root — never inside the code root (W7)."""
    # ``evidence`` is already a mutable root of the C11 scan, so a file written here
    # can never make the frozen envelope dirty.
    return paths.evidence_dir() / "chain" / "c11"


def record_c11_verdict(result: CheckResult) -> CheckResult:
    """Journal a C11 verdict into the content-addressed chain; WARN on failure.

    Contract (wave G1d, PLAN §1.4):

    * the verdict is computed BEFORE this call and this call never feeds back into
      it: the returned :class:`CheckResult` is a separate WARN entry, so no journal
      failure can change ``ok``, the CRITICAL filter or the exit code (W4);
    * the WHOLE attempt — resolving the chain root, building the record (hostname,
      timestamp, truncated detail) and the compare-and-swap append — sits inside one
      guarded region: a failure while *building* the record is a journal failure too,
      and a HEALTHY candidate must never be failed by its telemetry (B8);
    * BOTH verdicts are journalled — a journal of successes only cannot prove that a
      red check ever happened (W1);
    * the append is a compare-and-swap on HEAD with a bounded retry, because the
      pre- and post-start validators run concurrently by design (W3);
    * the record carries only anchors the check already measured, plus a truncated
      human-readable detail; no secret value and no path outside the evidence root
      ever reach it (W6);
    * an unreachable evidence root (``paths.evidence_dir()`` raising in an immutable
      deployment without a state root) is a WARN, not a traceback (W8).

    This is the wiring that turns the frozen ``antigona.chain`` into a working
    capability (ART-03): the C11 verdict stops being a stdout line only.
    """
    import socket
    from datetime import UTC, datetime

    try:
        from antigona.chain import ChainRecord, ChainStore, StaleHeadError
    except ImportError as exc:  # pragma: no cover - the chain package ships with the app
        # The import must not be inside the same clause that catches the append: a
        # failing import would leave those names unbound and the handler itself would
        # raise instead of returning the WARN it owes.
        return CheckResult(
            _EVIDENCE_CHAIN_CHECK, _SEV_WARN, False,
            f"C11 verdict journal unavailable: {exc}",
        )
    # W4/W8: EVERYTHING the journal does is inside this one guarded region — building
    # the record included.  ``socket.gethostname()``, ``datetime.now()`` and the
    # ``detail`` truncation are exactly as fallible as the write itself (and a shimmed
    # or container-broken hostname lookup raises ``OSError``); while they stood outside
    # the handler, a telemetry failure escaped ``run()``, skipped the report entirely
    # and made ``main()`` return the fail-closed 1 on a *green* envelope.  The handler
    # is deliberately ``Exception``-wide for the same reason: ``ChainError`` is a
    # ``RuntimeError`` subclass, so the former
    # ``(ChainError, OSError, RuntimeError, TypeError, ValueError)`` tuple is subsumed,
    # and no journal failure — known or exotic — may change the verdict or the exit code.
    try:
        payload: dict[str, object] = {
            "check": result.check,
            "ok": bool(result.ok),
            "detail": result.detail[:_C11_DETAIL_LIMIT],
            "recorded_at": datetime.now(UTC).isoformat(),
            "host": socket.gethostname(),
        }
        for key, value in (result.evidence or {}).items():
            payload[key] = value
        store = ChainStore(_c11_journal_root(), lineage_id=_C11_JOURNAL_LINEAGE)
        record = ChainRecord(operation=_C11_JOURNAL_OPERATION, payload=payload)
        for _attempt in range(_C11_JOURNAL_ATTEMPTS):
            expected = store.read_head()
            try:
                revision = store.append(expected, record)
            except StaleHeadError:
                # Lost CAS: a concurrent validator moved HEAD. Re-derive and retry.
                continue
            return CheckResult(
                _EVIDENCE_CHAIN_CHECK, _SEV_WARN, True,
                f"C11 verdict journalled at {revision}",
            )
    except Exception as exc:
        return CheckResult(
            _EVIDENCE_CHAIN_CHECK, _SEV_WARN, False,
            f"C11 verdict journal unavailable: {exc}",
        )
    return CheckResult(
        _EVIDENCE_CHAIN_CHECK, _SEV_WARN, False,
        f"C11 verdict journal refused after {_C11_JOURNAL_ATTEMPTS} compare-and-swap attempts",
    )



CANONICAL_ANTIGONA_UNITS = frozenset({
    "antigona-gateway.service", "antigona-orchestration.service",
    "antigona-verifier.service", "antigona-worker.service",
    "antigona-delivery.service", "antigona-bot.service", "antigona-status.service",
})

def _systemd_unit(pid: int) -> str:
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        return ""
    units = []
    for line in lines:
        match = re.fullmatch(r"0::/system\.slice/([^/]+\.service)", line)
        if match:
            units.append(match.group(1))
    return units[0] if len(units) == 1 else ""

def _is_legacy_status(p: ProcInfo) -> bool:
    try:
        argv = shlex.split(p.cmdline)
    except ValueError:
        return False
    # The legacy status server is an instance artifact that lives under the
    # *current user's* home directory. Derive it from the single home resolver
    # (ADR-007) instead of the literal "/root" so the exemption follows
    # HOME/ANTIGONA_HOME_DIR off-host. On this host home_dir() == "/root", so
    # the recognised argv is unchanged.
    legacy_argv = ["/usr/bin/python3", str(paths.home_dir() / "antigona-status" / "server.py")]
    return p.cwd == "/" and argv == legacy_argv


def _task_db_state(path: str) -> str:
    if not path.endswith("/telegram_turns.db"):
        return "healthy"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            turns = conn.execute("SELECT COUNT(*) FROM telegram_turns").fetchone()[0]
            queue = conn.execute("SELECT COUNT(*) FROM telegram_queue").fetchone()[0]
        return "empty-healthy" if turns == 0 and queue == 0 else "stale-active"
    except (OSError, sqlite3.Error):
        return "unreadable"

def check_post(procs: list[ProcInfo]) -> list[CheckResult]:
    results = _count_checks(procs)

    # C10 — единый cwd (Runtime Provenance Chain)
    antigona = [p for p in procs if re.search(r"antigona", p.cmdline)]
    legacy_excluded = [p for p in antigona if _is_legacy_status(p)]
    provenance = [p for p in antigona if p not in legacy_excluded]
    bad_cwd = [(p.pid, p.cwd) for p in provenance if p.cwd != str(paths.project_root())]
    results.append(CheckResult(
        check="contract:C10:cwd",
        severity=_SEV_CRITICAL,
        ok=not bad_cwd,
        detail=f"все процессы cwd=={paths.project_root()}" if not bad_cwd
               else f"расхождение cwd: {bad_cwd}",
    ))

    # C11 — Deployment Worktree Integrity Guard
    results.append(check_worktree_integrity())

    # C6 — открытые SQLite
    dbs = open_db_files(procs)
    allowed = {
        str(paths.database_path()), str(paths.sessions_db_path()),
        str(paths.memory_db_path()), str(paths.turn_ledger_path()),
    }
    # Ownership-ledger SQLite files are canonical (``<owner_dir>/ownership/*.db``),
    # created by the ownership authority for protected writes — allow them.
    try:
        from antigona.ownership.wiring import default_ownership_dir
        _own_dir = str(default_ownership_dir())
        allowed.update(
            d for d in dbs if d.startswith(_own_dir + "/") and d.endswith(".db")
        )
    except Exception:  # pragma: no cover - defensive; ownership optional
        pass
    states = {d: _task_db_state(d) for d in dbs if d in allowed}
    unexpected = sorted(d for d in dbs if d not in allowed)
    stale = sorted(d for d, state in states.items() if state in {"stale-active", "unreadable"})
    results.append(CheckResult(
        check="contract:C6:db",
        severity=_SEV_CRITICAL,
        ok=not unexpected and not stale,
        detail=f"открытые .db: {sorted(dbs)}" if not unexpected
               else f"неожиданные/устаревшие .db: {unexpected + stale}; task DB state={states}",
    ))

    # C7/C9 — Owner Identity и Bot Token
    owner = os.environ.get("ANTIGONA_OWNER_ID", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    results.append(CheckResult(
        check="contract:C7:owner_id",
        severity=_SEV_CRITICAL,
        ok=bool(owner),
        detail=f"ANTIGONA_OWNER_ID={'set' if owner else 'MISSING'}",
    ))
    results.append(CheckResult(
        check="contract:C9:bot_token",
        severity=_SEV_CRITICAL,
        ok=bool(token) and token != "dummy-token",
        detail=f"TELEGRAM_BOT_TOKEN={'set' if token else 'MISSING'}"
               + (" (dummy-token!)" if token == "dummy-token" else ""),
    ))

    # C8 — PIN
    pin = os.environ.get("ANTIGONA_PIN", "")
    results.append(CheckResult(
        check="contract:C8:pin",
        severity=_SEV_CRITICAL,
        ok=bool(pin),
        detail=f"ANTIGONA_PIN={'set' if pin else 'MISSING'}",
    ))

    # Процессы-сироты (PPID==1)
    now = time.time()
    owned = {p.pid: _systemd_unit(p.pid) for p in antigona}
    orphans = [p.pid for p in antigona if p.ppid == 1 and owned[p.pid] not in CANONICAL_ANTIGONA_UNITS
               and p.pid != os.getpid() and now - p.start > 300]
    results.append(CheckResult(
        check="orphans",
        severity=_SEV_WARN,
        ok=not orphans,
        detail=f"процессы-сироты (PPID=1, >5 мин): {orphans}" if orphans
               else "сирот нет",
    ))
    return results



def run(check: str) -> int:
    if check in ("manifest", "c11", "worktree"):
        results = [check_worktree_integrity()]
    elif check == "pre":
        procs = _all_procs()
        results = check_pre(procs)
    else:
        procs = _all_procs()
        results = check_post(procs)
    critical = [r for r in results if r.severity == _SEV_CRITICAL and not r.ok]
    # Wave G1d: journal the C11 verdict — BOTH verdicts, red and green.  The loop
    # runs on a copy so appending the journal entry cannot affect the walk, and the
    # journal entry is a WARN: it can never enter ``critical`` above.  One call-site
    # covers ``--check=manifest|c11|worktree`` and the ``post`` branch (C11 is added
    # to ``results`` there); removing it + ``record_c11_verdict`` returns the
    # validator to its pre-G1d behaviour bit for bit.
    for result in list(results):
        if result.check == _C11_CHECK:
            results.append(record_c11_verdict(result))
            break
    print("=== Runtime Validator —", check, "===")
    for r in sorted(results, key=lambda x: (x.severity != _SEV_CRITICAL, x.check)):
        flag = "❌" if not r.ok else "✅"
        print(f"  [{r.severity:<8}] {flag} {r.check}: {r.detail}")
    if critical:
        print("⛔ КРИТИЧЕСКИЕ НАРУШЕНИЯ — запуск отменён.")
        return 1
    print("✅ Валидация пройдена.")
    return 0


def main() -> int:
    if os.environ.get("ANTIGONA_SKIP_VALIDATOR") == "1":
        print("Runtime Validator: пропущен (ANTIGONA_SKIP_VALIDATOR=1)")
        return 0
    ap = argparse.ArgumentParser(description="Antigona Runtime Validator")
    ap.add_argument("--check", choices=["pre", "post", "manifest", "c11", "worktree"], default="post")
    args = ap.parse_args()
    try:
        return run(args.check)
    except Exception as exc:
        print(f"Runtime Validator: сбой валидатора ({exc}) — fail-closed exit 1.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
