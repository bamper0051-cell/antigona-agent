#!/usr/bin/env python3
"""Antigona permanent Review & Evidence Pipeline (read-only).

See README.md for the process contract.

Examples:
  python3 scripts/review_evidence_pipeline.py --mode quick
  python3 scripts/review_evidence_pipeline.py --mode standard --only sessions,tools,approvals
  python3 scripts/review_evidence_pipeline.py --mode full --require-live
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Package imports (scripts/ on path)
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from review_evidence import __version__ as PIPELINE_VERSION  # noqa: E402
from review_evidence.collectors import (  # noqa: E402
    CollectorResult,
    Finding,
    collect_docs_ssot,
    collect_git,
    collect_parity,
    collect_path_inventory,
    collect_portrait_heuristic,
    collect_providers,
    collect_runtime,
    detect_approval_grant,
    detect_policy_fail_open,
    detect_sessions_last_n,
    load_subsystems_yaml,
    run_pytest,
    utc_now,
)


def repo_root_from_cwd() -> Path:
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "src" / "antigona").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    # Fallback: relative to this script
    return Path(__file__).resolve().parents[1]


def short_sha(full: str) -> str:
    return (full or "nogit")[:8]


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sha256sums(pack: Path) -> None:
    lines: list[str] = []
    for p in sorted(pack.rglob("*")):
        if not p.is_file():
            continue
        if p.name == "SHA256SUMS.txt":
            continue
        rel = p.relative_to(pack).as_posix()
        lines.append(f"{sha256_file(p)}  {rel}")
    write_text(pack / "SHA256SUMS.txt", "\n".join(lines) + ("\n" if lines else ""))


def decide_verdict(
    findings: list[Finding],
    *,
    mode: str,
    collectors_ok: bool,
    tests_failed: int,
) -> str:
    sev = {f.severity for f in findings}
    if not collectors_ok:
        return "EVIDENCE_BLOCKED"
    if "critical" in sev:
        return "EVIDENCE_FAIL"
    if mode in {"security", "full"} and "high" in sev:
        return "EVIDENCE_FAIL"
    if tests_failed > 0 and mode in {"full", "standard", "security"}:
        # tests failed → fail for gate modes
        return "EVIDENCE_FAIL"
    if findings or tests_failed > 0:
        return "EVIDENCE_PASS_WITH_FINDINGS"
    return "EVIDENCE_PASS"


def render_md_table(rows: list[list[str]], headers: list[str]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def run_pipeline(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve() if args.repo else repo_root_from_cwd()
    if not (repo / "src" / "antigona").is_dir():
        print(f"ERROR: not an Antigona repo root: {repo}", file=sys.stderr)
        return 2

    cfg = load_subsystems_yaml(repo)
    modes = cfg.get("modes") or {}
    all_subs = {s["id"]: s for s in cfg.get("subsystems") or []}

    if args.mode not in modes:
        print(f"ERROR: unknown mode {args.mode!r}; choose from {sorted(modes)}", file=sys.stderr)
        return 2

    selected = list(modes[args.mode])
    if args.only:
        only = [x.strip() for x in args.only.split(",") if x.strip()]
        selected = [s for s in only if s in all_subs]
        # always keep git+runtime if present in registry
        for mandatory in ("git", "runtime"):
            if mandatory in all_subs and mandatory not in selected:
                selected.insert(0, mandatory)

    # Resolve always subsystems
    for s in all_subs.values():
        if s.get("always") and s["id"] not in selected:
            selected.insert(0, s["id"])

    # de-dup preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for s in selected:
        if s not in seen and s in all_subs:
            seen.add(s)
            ordered.append(s)
    selected = ordered

    git_preview = collect_git(repo)
    head = git_preview.data.get("head") or "unknown"
    branch = git_preview.data.get("branch") or "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    pack_name = f"REVIEW_{stamp}_{args.mode}_{short_sha(head)}"
    out_root = Path(args.out_dir) if args.out_dir else (repo / "evidence")
    pack = out_root / pack_name
    pack.mkdir(parents=True, exist_ok=False)
    (pack / "RAW").mkdir()
    (pack / "RAW_TEST_OUTPUT").mkdir()
    (pack / "DIFFS").mkdir()

    log_lines: list[str] = [
        f"# RAW_COMMANDS.log — Review & Evidence Pipeline v{PIPELINE_VERSION}",
        f"started_at={utc_now()}",
        f"mode={args.mode}",
        f"repo={repo}",
        f"read_only=true",
        f"subsystems={','.join(selected)}",
        "",
    ]

    results: dict[str, CollectorResult] = {}
    all_findings: list[Finding] = []

    def record(res: CollectorResult) -> None:
        results[res.subsystem] = res
        all_findings.extend(res.findings)
        write_text(pack / "RAW" / f"subsystem_{res.subsystem}.json", json.dumps({
            "subsystem": res.subsystem,
            "ok": res.ok,
            "summary": res.summary,
            "data": res.data,
            "findings": [f.to_dict() for f in res.findings],
        }, indent=2, ensure_ascii=False) + "\n")
        if res.raw_text:
            write_text(pack / "RAW" / f"subsystem_{res.subsystem}.txt", res.raw_text)
        log_lines.append(f"## collector {res.subsystem}: ok={res.ok} {res.summary}")

    # --- Collectors ---
    record(git_preview)
    write_text(pack / "RAW" / "git_snapshot.txt", git_preview.raw_text)

    if "runtime" in selected:
        rt = collect_runtime(repo, require_live=bool(args.require_live))
        record(rt)
        write_text(pack / "RAW" / "runtime_snapshot.txt", rt.raw_text)

    if "sessions" in selected:
        record(detect_sessions_last_n(repo))

    if "tools" in selected or "security" in selected:
        record(detect_policy_fail_open(repo))

    if "approvals" in selected or "security" in selected:
        record(detect_approval_grant(repo))

    if "parity" in selected:
        record(collect_parity(repo))

    if "portrait" in selected:
        record(collect_portrait_heuristic(repo))

    if "providers" in selected:
        record(collect_providers(repo))

    if "docs_ssot" in selected:
        record(collect_docs_ssot(repo))

    # Path inventory for remaining subsystems
    for sid in selected:
        if sid in results:
            continue
        sub = all_subs[sid]
        paths = list(sub.get("paths") or [])
        record(collect_path_inventory(repo, sid, paths))

    # --- Tests ---
    test_stats: dict[str, Any] = {}
    test_targets: list[str] = []
    for sid in selected:
        for t in all_subs[sid].get("tests") or []:
            if t not in test_targets:
                test_targets.append(t)

    if args.skip_tests:
        test_stats = {"ran": False, "reason": "--skip-tests", "passed": 0, "failed": 0, "skipped": 0}
        log_lines.append("tests skipped by flag")
    else:
        # architecture always on standard+
        if args.mode in {"standard", "full", "security"} and "tests/architecture" not in test_targets:
            test_targets.append("tests/architecture")
        if args.mode == "full":
            # broader but still bounded
            for extra in ("tests/gateway", "tests/unit/test_brain_concurrency.py"):
                if extra not in test_targets:
                    test_targets.append(extra)

        out_file = pack / "RAW_TEST_OUTPUT" / f"pytest_{args.mode}.txt"
        log_lines.append(f"$ pytest targets={test_targets}")
        test_stats = run_pytest(
            repo,
            test_targets,
            out_file=out_file,
            timeout=int(args.test_timeout),
        )
        log_lines.append(
            f"pytest exit={test_stats.get('exit_code')} "
            f"passed={test_stats.get('passed')} failed={test_stats.get('failed')} "
            f"skipped={test_stats.get('skipped')}"
        )
        if test_stats.get("failed", 0) > 0:
            all_findings.append(
                Finding(
                    id="TESTS_FAILED",
                    severity="high",
                    subsystem="tests",
                    title="Pytest reported failures",
                    detail=(
                        f"failed={test_stats.get('failed')} "
                        f"passed={test_stats.get('passed')} "
                        f"see RAW_TEST_OUTPUT/pytest_{args.mode}.txt"
                    ),
                    evidence=f"RAW_TEST_OUTPUT/pytest_{args.mode}.txt",
                )
            )

    collectors_ok = all(
        results[s].ok for s in results if s in {"git"}  # blocked only if core collectors explode
    )
    # If git critical missing head → blocked
    if any(f.id == "GIT_HEAD_MISSING" for f in all_findings):
        collectors_ok = False

    verdict = decide_verdict(
        all_findings,
        mode=args.mode,
        collectors_ok=collectors_ok,
        tests_failed=int(test_stats.get("failed") or 0),
    )

    # --- Render reports ---
    matrix_rows: list[list[str]] = []
    for sid in selected:
        res = results.get(sid)
        if not res:
            matrix_rows.append([sid, "—", "NOT_RUN", "", ""])
            continue
        crit = sum(1 for f in res.findings if f.severity == "critical")
        high = sum(1 for f in res.findings if f.severity == "high")
        matrix_rows.append(
            [
                sid,
                "ok" if res.ok else "FAIL",
                res.summary.replace("|", "/"),
                str(crit),
                str(high),
            ]
        )

    write_text(
        pack / "04_SUBSYSTEM_MATRIX.md",
        "# 04 — Subsystem matrix\n\n"
        + render_md_table(
            matrix_rows,
            ["subsystem", "status", "summary", "critical", "high"],
        )
        + "\n",
    )

    git_d = results["git"].data
    write_text(
        pack / "02_GIT_TOPOLOGY.md",
        "\n".join(
            [
                "# 02 — Git topology",
                "",
                f"- **branch:** `{git_d.get('branch')}`",
                f"- **HEAD:** `{git_d.get('head')}`",
                f"- **dirty tracked:** {len(git_d.get('dirty_tracked') or [])}",
                f"- **untracked:** {len(git_d.get('untracked') or [])}",
                f"- **worktrees:** {len(git_d.get('worktrees') or [])}",
                "",
                "## status",
                "```",
                str(git_d.get("status_sb") or ""),
                "```",
                "",
                "## recent log",
                "```",
                "\n".join(git_d.get("log_20") or []),
                "```",
                "",
                "Full raw: `RAW/git_snapshot.txt`",
                "",
            ]
        ),
    )

    rt = results.get("runtime")
    write_text(
        pack / "13_RUNTIME_LIVE_TRUTH.md",
        "\n".join(
            [
                "# 13 — Runtime / live truth",
                "",
                f"- gateway_health_ok: **{(rt.data.get('gateway_health_ok') if rt else False)}**",
                f"- gateway_port: `{(rt.data.get('gateway_port') if rt else None)}`",
                f"- hermes PYTHONPATH hits: {len((rt.data.get('pythonpath_hermes') if rt else []) or [])}",
                "",
                "Raw: `RAW/runtime_snapshot.txt`",
                "",
            ]
        ),
    )

    write_text(
        pack / "11_FULL_TEST_RESULTS.md",
        "\n".join(
            [
                "# 11 — Test results",
                "",
                "```json",
                json.dumps(test_stats, indent=2, default=str),
                "```",
                "",
                f"Log: `RAW_TEST_OUTPUT/pytest_{args.mode}.txt`",
                "",
            ]
        ),
    )

    # Specialized slices
    if "sessions" in results:
        s = results["sessions"]
        write_text(
            pack / "07_SESSIONS_MEMORY_SLICE.md",
            "# 07 — Sessions / memory slice\n\n"
            f"**Summary:** {s.summary}\n\n"
            f"```json\n{json.dumps(s.data, indent=2, ensure_ascii=False)}\n```\n",
        )
    if "tools" in results or "approvals" in results or "security" in selected:
        chunks = ["# 06 — Security slice\n"]
        for sid in ("tools", "approvals", "security"):
            if sid in results:
                r = results[sid]
                chunks.append(f"## {sid}\n\n{r.summary}\n\n```json\n{json.dumps(r.data, indent=2, ensure_ascii=False)}\n```\n")
        write_text(pack / "06_SECURITY_SLICE.md", "\n".join(chunks))

    if "parity" in results:
        p = results["parity"]
        write_text(
            pack / "08_CLI_TELEGRAM_PARITY.md",
            "# 08 — CLI ↔ Telegram parity\n\n"
            f"**Summary:** {p.summary}\n\n"
            f"```json\n{json.dumps(p.data, indent=2, ensure_ascii=False)}\n```\n",
        )

    if "portrait" in results or "cli" in results:
        parts = ["# 09 — CLI / Portrait slice\n"]
        for sid in ("cli", "portrait"):
            if sid in results:
                r = results[sid]
                parts.append(f"## {sid}\n\n{r.summary}\n\n```json\n{json.dumps(r.data, indent=2, ensure_ascii=False)}\n```\n")
        write_text(pack / "09_CLI_PORTRAIT_SLICE.md", "\n".join(parts))

    # Findings
    finding_lines = ["# 12 — Findings\n"]
    if not all_findings:
        finding_lines.append("_No findings._\n")
    else:
        finding_lines.append(
            render_md_table(
                [
                    [f.id, f.severity, f.subsystem, f.title.replace("|", "/")]
                    for f in all_findings
                ],
                ["id", "severity", "subsystem", "title"],
            )
        )
        finding_lines.append("")
        for f in all_findings:
            finding_lines.append(f"## {f.id}\n\n- severity: `{f.severity}`\n- subsystem: `{f.subsystem}`\n- {f.detail}\n- evidence: `{f.evidence}`\n")
    write_text(pack / "12_FINDINGS.md", "\n".join(finding_lines) + "\n")

    # Truth + executive
    crit = [f for f in all_findings if f.severity == "critical"]
    write_text(
        pack / "01_CURRENT_TRUTH.md",
        "\n".join(
            [
                "# 01 — Current truth",
                "",
                f"- Generated: `{utc_now()}`",
                f"- Mode: `{args.mode}`",
                f"- Branch: `{branch}`",
                f"- HEAD: `{head}`",
                f"- Pipeline: v{PIPELINE_VERSION}",
                f"- Read-only: **true**",
                f"- Critical findings: **{len(crit)}**",
                "",
                "Subsystem matrix: `04_SUBSYSTEM_MATRIX.md`",
                "Findings: `12_FINDINGS.md`",
                "",
                "Documents and prior reports are **claims**. This pack is the measured state for this run.",
                "",
            ]
        ),
    )

    write_text(
        pack / "00_EXECUTIVE_SUMMARY.md",
        "\n".join(
            [
                "# 00 — Executive summary",
                "",
                f"**Verdict: `{verdict}`**",
                "",
                f"- mode: `{args.mode}`",
                f"- head: `{short_sha(head)}` (`{branch}`)",
                f"- subsystems: {', '.join(selected)}",
                f"- findings: {len(all_findings)} (critical={len(crit)})",
                f"- tests: passed={test_stats.get('passed')} failed={test_stats.get('failed')} skipped={test_stats.get('skipped')}",
                "",
                "## Top findings",
                "",
                *(
                    [f"- **{f.id}** ({f.severity}): {f.title}" for f in all_findings[:10]]
                    or ["- none"]
                ),
                "",
                "This pack was produced by the permanent Review & Evidence Pipeline. "
                "It does not mutate the repository or live stack.",
                "",
            ]
        ),
    )

    next_actions = ["# 16 — Next actions\n"]
    if verdict == "EVIDENCE_PASS":
        next_actions.append("- No blocking findings. Safe to proceed with planned change under normal review.\n")
    else:
        next_actions.append("- Address critical/high findings before claiming subsystem readiness.\n")
        for f in all_findings:
            if f.severity in {"critical", "high"}:
                next_actions.append(f"- [ ] {f.id}: {f.title}\n")
    next_actions.append(
        "\n- Re-run: `python3 scripts/review_evidence_pipeline.py --mode "
        f"{args.mode}`\n"
    )
    write_text(pack / "16_NEXT_ACTIONS.md", "".join(next_actions))

    write_text(
        pack / "14_DOC_SSOT_HINTS.md",
        "# 14 — Doc SSOT hints\n\n"
        "See subsystem `docs_ssot` if selected. Canonical process doc: "
        "`README.md`.\n",
    )

    write_text(
        pack / "10_TEST_INVENTORY.md",
        "# 10 — Test inventory (this run)\n\n"
        f"Targets requested:\n\n```\n{json.dumps(test_targets, indent=2)}\n```\n",
    )

    write_text(
        pack / "FINAL_VERDICT.md",
        "\n".join(
            [
                "# FINAL VERDICT",
                "",
                f"## `{verdict}`",
                "",
                f"- pipeline_version: {PIPELINE_VERSION}",
                f"- mode: {args.mode}",
                f"- head: {head}",
                f"- branch: {branch}",
                f"- finished_at: {utc_now()}",
                "",
                "Allowed verdicts: EVIDENCE_PASS | EVIDENCE_PASS_WITH_FINDINGS | EVIDENCE_FAIL | EVIDENCE_BLOCKED",
                "",
            ]
        ),
    )

    finished = utc_now()
    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "mode": args.mode,
        "started_at": stamp,
        "finished_at": finished,
        "repo_root": str(repo),
        "git_head": head,
        "git_branch": branch,
        "subsystems": selected,
        "verdict": verdict,
        "findings": [f.to_dict() for f in all_findings],
        "tests": test_stats,
        "read_only": True,
        "pack": str(pack),
    }
    write_text(pack / "MANIFEST.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    log_lines.append(f"finished_at={finished}")
    log_lines.append(f"verdict={verdict}")
    log_lines.append(f"pack={pack}")
    write_text(pack / "RAW_COMMANDS.log", "\n".join(log_lines) + "\n")

    write_sha256sums(pack)

    # Console summary
    print(f"PACK={pack}")
    print(f"VERDICT={verdict}")
    print(f"FINDINGS={len(all_findings)} critical={len(crit)}")
    print(f"TESTS passed={test_stats.get('passed')} failed={test_stats.get('failed')} skipped={test_stats.get('skipped')}")
    for f in all_findings:
        if f.severity in {"critical", "high"}:
            print(f"  [{f.severity}] {f.id}: {f.title}")

    if verdict in {"EVIDENCE_FAIL", "EVIDENCE_BLOCKED"}:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Antigona permanent Review & Evidence Pipeline (read-only)",
    )
    p.add_argument(
        "--mode",
        default="standard",
        choices=["quick", "standard", "full", "security", "portrait", "parity"],
        help="Pipeline mode (default: standard)",
    )
    p.add_argument("--only", default="", help="Comma-separated subsystem ids to limit scope")
    p.add_argument("--repo", default="", help="Repo root (default: auto-detect)")
    p.add_argument("--out-dir", default="", help="Evidence parent directory (default: <repo>/evidence)")
    p.add_argument("--require-live", action="store_true", help="Fail if gateway /health is down")
    p.add_argument("--skip-tests", action="store_true", help="Skip pytest (collectors only)")
    p.add_argument("--test-timeout", default="600", help="Pytest timeout seconds (default 600)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
