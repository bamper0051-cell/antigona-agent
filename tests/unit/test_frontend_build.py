"""Tests for the frontend_build tool (nodejs.md section 17 acceptance)."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from antigona.tools.frontend_build import (
    _detect_package_manager,
    _handle_frontend_build,
    _resolve_project,
)


def _write_project(base: Path, *, with_pkg: bool = True, pm: str = "npm") -> Path:
    proj = base / "fe"
    proj.mkdir(parents=True, exist_ok=True)
    if with_pkg:
        (proj / "package.json").write_text(json.dumps({"name": "fe", "version": "0.0.1"}))
    if pm == "pnpm":
        (proj / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'")
    else:
        (proj / "package-lock.json").write_text("{}")
    return proj


# ── package manager detection ────────────────────────────────────────────────


def test_detects_npm_from_package_lock(tmp_path: Path) -> None:
    assert _detect_package_manager(_write_project(tmp_path, pm="npm")) == "npm"


def test_detects_pnpm_from_lockfile(tmp_path: Path) -> None:
    assert _detect_package_manager(_write_project(tmp_path, pm="pnpm")) == "pnpm"


def test_package_manager_field_overrides(tmp_path: Path) -> None:
    proj = tmp_path / "fe"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text(
        json.dumps({"name": "fe", "packageManager": "pnpm@9.0.0"})
    )
    assert _detect_package_manager(proj) == "pnpm"


# ── allowlist / traversal / missing package.json ─────────────────────────────


def test_resolve_rejects_outside_allowlist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [tmp_path / "fe"],
    )
    root, err = _resolve_project(str(tmp_path / "other"))
    assert root is None
    assert "вне allowlist" in (err or "")


def test_resolve_rejects_traversal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [tmp_path / "fe"],
    )
    root, err = _resolve_project("../fe")
    assert root is None
    assert "вне allowlist" in (err or "") or "package.json" in (err or "")


def test_resolve_rejects_missing_package_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [tmp_path / "fe"],
    )
    root, err = _resolve_project(str(tmp_path / "fe"))
    assert root is None
    assert "package.json" in (err or "")


# ── handler: invalid inputs fail closed ──────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_rejects_traversal_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [tmp_path / "fe"],
    )
    res = json.loads(await _handle_frontend_build(project="../../etc"))
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_handler_rejects_bad_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [tmp_path / "fe"],
    )
    _write_project(tmp_path)
    res = json.loads(await _handle_frontend_build(project=str(tmp_path / "fe"), mode="rm -rf"))
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_handler_rejects_missing_node(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [tmp_path / "fe"],
    )
    _write_project(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    res = json.loads(await _handle_frontend_build(project=str(tmp_path / "fe")))
    assert res["ok"] is False
    assert "node" in res["error"]


# ── successful build (real toolchain) ────────────────────────────────────────


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm unavailable")
@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason='runtime invokes npm (not npm.cmd); frontend build subprocess not Windows-ready (tests-only Wave 4)')
async def test_successful_build_ok(tmp_path: Path, monkeypatch) -> None:
    proj = tmp_path / "fe"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text(
        json.dumps({
            "name": "fe", "version": "0.0.1",
            "packageManager": "npm@10.9.8", "type": "module",
            "scripts": {"build": "tsc -b && vite build", "typecheck": "tsc -b --noEmit"},
            "devDependencies": {"typescript": "^5.5.0", "vite": "^5.4.0"},
        })
    )
    (proj / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2020","module":"ESNext","moduleResolution":"bundler","lib":["ES2020","DOM"],"strict":true,"noEmit":true,"skipLibCheck":true},"include":["src"]}'
    )
    (proj / "vite.config.ts").write_text('import { defineConfig } from "vite"; export default defineConfig({ build: { outDir: "dist" } });')
    (proj / "index.html").write_text('<!doctype html><html><body><div id="app"></div><script type="module" src="/src/main.ts"></script></body></html>')
    (proj / "src").mkdir()
    (proj / "src" / "main.ts").write_text('document.getElementById("app")!.textContent = "ok";')
    # Hermetic: install dev deps (typescript/vite) regardless of ambient
    # NODE_ENV=production, which would otherwise make npm omit them → tsc
    # missing → `npm run typecheck` exits 127.
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", "--include=dev"],
        cwd=proj,
        capture_output=True,
        check=False,
    )

    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [proj],
    )
    res = json.loads(await _handle_frontend_build(project=str(proj)))
    assert res["ok"] is True, res
    assert res["dist_exists"] is True
    assert any("index.html" in f for f in res["files"])


# ── concurrent duplicate build blocked ───────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_duplicate_blocked(tmp_path: Path, monkeypatch) -> None:
    proj = _write_project(tmp_path)
    monkeypatch.setattr(
        "antigona.tools.frontend_build._allowed_projects",
        lambda: [proj],
    )

    from antigona.tools import frontend_build

    holder = asyncio.get_event_loop().create_future()
    frontend_build._BUILD_LOCKS[proj] = holder

    # While a build is held open, a second call must be refused.
    res = json.loads(await _handle_frontend_build(project=str(proj)))
    assert res["ok"] is False
    assert "уже выполняется" in res["error"]
    if not holder.done():
        holder.set_result(True)


# ── integration: advertised + dispatch through the common tool path ──────────


@pytest.mark.asyncio
async def test_frontend_build_advertised_in_integration_block() -> None:
    from antigona.conversation.dialogue_engine import DialogueEngine
    from antigona.tools.registry import ToolRegistry, register_builtins

    reg = ToolRegistry()
    register_builtins(reg)
    engine = DialogueEngine(registry=reg)
    try:
        block = engine._integration_tools_block()
    finally:
        await engine.close()
    assert "frontend_build" in block


@pytest.mark.asyncio
async def test_frontend_build_is_registered_and_injection_rejected() -> None:
    from antigona.tools.registry import ToolRegistry, register_builtins

    reg = ToolRegistry()
    register_builtins(reg)
    assert "frontend_build" in [t.name for t in reg.list()]
    # No arbitrary command string parameter exists in the schema.
    schema = reg._tools["frontend_build"].schema
    props = set(schema.get("properties", {}).keys())
    assert "command" not in props
    assert "additionalProperties" in schema and schema["additionalProperties"] is False
