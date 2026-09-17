"""frontend_build — controlled Antigona tool for TypeScript/Vite frontend builds.

NOT arbitrary shell: the tool has a limited contract (project, mode,
install_dependencies, clean_build) and operates only on allowlisted project
roots. It detects the project's own package manager, runs the project's own
build scripts through local binaries, and returns a structured result string
suitable for both CLI and Telegram. TypeScript failures and Vite failures are
classified distinctly. Traversal/symlink escapes and arbitrary command
injection are rejected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from antigona.core import paths

logger = logging.getLogger(__name__)

#: Tools allowed to run through the build step. Everything else is refused.
_ALLOWED_PM_CMDS = ("npm", "pnpm", "yarn", "bun")
_DEFAULT_TIMEOUT = 300.0
_MAX_OUTPUT = 6000
_NODE_BINARIES = ("node", "npx", "npm", "pnpm", "yarn", "bun", "tsc", "vite")


def _allowed_projects() -> list[Path]:
    """Allowlisted buildable frontend roots (resolved, canonical)."""
    root = Path(paths.project_root())
    return [root / "frontend_ts"]


def _detect_package_manager(project: Path) -> str:
    """Detect the project's package manager from lockfile / packageManager."""
    if (project / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project / "yarn.lock").exists():
        return "yarn"
    if (project / "bun.lock").exists() or (project / "bun.lockb").exists():
        return "bun"
    if (project / "package-lock.json").exists():
        return "npm"
    # packageManager field (e.g. "pnpm@9.0.0" or "npm@10.9.8")
    pkg = _read_package_json(project)
    pm: str = str((pkg or {}).get("packageManager", "") or "")
    if pm:
        name = pm.split("@", 1)[0].strip().lower()
        if name in _ALLOWED_PM_CMDS:
            return name
    return "npm"


def _read_package_json(project: Path) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = json.loads(
            (project / "package.json").read_text(encoding="utf-8")
        )
        return data
    except Exception:
        return None


def _resolve_project(project: str) -> tuple[Path | None, str | None]:
    """Resolve the requested project against the allowlist. Returns (root, error)."""
    raw = (project or "").strip()
    if not raw:
        return None, "project не указан"
    requested = Path(raw)
    if requested.is_absolute():
        resolved = requested
    else:
        resolved = (Path(paths.project_root()) / requested)
    try:
        resolved = resolved.resolve(strict=False)
    except OSError:
        return None, "не удалось разрешить путь"
    allowed = [p.resolve() for p in _allowed_projects()]
    if resolved not in allowed:
        return None, (
            f"проект вне allowlist: {resolved} (разрешены: "
            + ", ".join(str(p) for p in allowed) + ")"
        )
    if not (resolved / "package.json").exists():
        return None, f"в {resolved} нет package.json"
    return resolved, None


def _build_env() -> dict[str, str]:
    """Environment for build subprocesses.

    ``NODE_ENV=production`` is stripped so that ``npm``/``pnpm``/``yarn``/``bun``
    install devDependencies (typescript, vite, …) regardless of the host's
    ambient mode. A frontend build is a dev-time operation by definition: it
    must have dev dependencies available, and ``npm run <script>`` must not run
    in production-omit mode. Only this key is removed; everything else is
    inherited.
    """
    env = dict(os.environ)
    env.pop("NODE_ENV", None)
    return env


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a command with a hard timeout and bounded output capture."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_build_env(),
    )


def _classify_build_failure(cmd: str, stdout: str, stderr: str) -> str:
    """Distinguish TypeScript failures from Vite/build failures."""
    combined = (stdout + "\n" + stderr).lower()
    if "tsc" in cmd or "error ts" in combined or "typescript" in combined:
        if "error ts" in combined or "error ts" in stderr.lower():
            return "typescript"
    if "vite" in cmd or "vite build" in combined or "rollup" in combined:
        return "vite"
    return "build"


async def _handle_frontend_build(
    project: str = "frontend_ts",
    mode: str = "check_and_build",
    install_dependencies: bool = False,
    clean_build: bool = False,
    _owner_id: str = "",
    **_kw: Any,
) -> str:
    """Build/check an allowlisted frontend project. Returns a JSON string."""
    root, err = _resolve_project(project)
    if err:
        return json.dumps({"ok": False, "error": err, "project": project})
    assert root is not None

    allowed_modes = ("check", "build", "check_and_build")
    if mode not in allowed_modes:
        return json.dumps({"ok": False, "error": f"mode должен быть один из {allowed_modes}"})

    # Node / package-manager availability
    for bin_name in ("node",):
        if shutil.which(bin_name) is None:
            return json.dumps({"ok": False, "error": f"{bin_name} не найден в PATH"})

    pm = _detect_package_manager(root)
    if pm not in _ALLOWED_PM_CMDS:
        return json.dumps({"ok": False, "error": f"неподдерживаемый package manager: {pm}"})
    if shutil.which(pm) is None:
        return json.dumps({"ok": False, "error": f"{pm} не найден в PATH"})

    # Per-project concurrency guard: a build already running for this project is refused.
    _LOCK_KEY = "antigona.frontend_build"
    existing = _BUILD_LOCKS.get(root)
    if existing is not None and not existing.done():
        return json.dumps({"ok": False, "error": "уже выполняется сборка для этого проекта"})
    lock = asyncio.get_event_loop().create_future()
    _BUILD_LOCKS[root] = lock
    try:
        # install_dependencies (reproducible)
        if install_dependencies:
            if pm == "npm" and (root / "package-lock.json").exists():
                install = ["npm", "ci"]
            else:
                install = [pm, "install"]
            inst = await asyncio.to_thread(
                _run, install, root, _DEFAULT_TIMEOUT
            )
            if inst.returncode != 0:
                return json.dumps({
                    "ok": False, "error": "установка зависимостей не удалась",
                    "stage": "install", "exit": inst.returncode,
                    "tail": (inst.stderr or inst.stdout)[-_MAX_OUTPUT:],
                })

        # typecheck (check mode or always for check_and_build)
        if mode in ("check", "check_and_build"):
            tc = await asyncio.to_thread(
                _run, [pm, "run", "typecheck"], root, _DEFAULT_TIMEOUT
            )
            if tc.returncode != 0:
                return json.dumps({
                    "ok": False,
                    "error": "TypeScript проверка не прошла",
                    "stage": "typescript",
                    "exit": tc.returncode,
                    "tail": (tc.stderr or tc.stdout)[-_MAX_OUTPUT:],
                })
            if mode == "check":
                return json.dumps({
                    "ok": True, "mode": "check", "project": project,
                    "result": "TypeScript проверка прошла",
                })

        # build (clean_build empties dist first via vite emptyOutDir)
        if clean_build:
            _run(["rm", "-rf", str(root / "dist")], root, 30.0)
        bld = await asyncio.to_thread(
            _run, [pm, "run", "build"], root, _DEFAULT_TIMEOUT
        )
        if bld.returncode != 0:
            kind = _classify_build_failure(
                " ".join(bld.args), bld.stdout, bld.stderr
            )
            return json.dumps({
                "ok": False,
                "error": "сборка не удалась",
                "stage": kind,
                "exit": bld.returncode,
                "tail": (bld.stderr or bld.stdout)[-_MAX_OUTPUT:],
            })

        dist = root / "dist"
        files = sorted(str(p.relative_to(root)) for p in dist.rglob("*") if p.is_file()) if dist.exists() else []
        return json.dumps({
            "ok": True,
            "mode": mode,
            "project": project,
            "package_manager": pm,
            "dist_exists": dist.exists(),
            "files": files,
            "result": f"сборка прошла успешно ({len(files)} файлов в dist/)",
        })
    finally:
        if not lock.done():
            lock.set_result(True)


_BUILD_LOCKS: dict[Path, Any] = {}


_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "default": "frontend_ts",
                    "description": "allowlisted frontend root (relative to project root)"},
        "mode": {"type": "string", "enum": ["check", "build", "check_and_build"],
                 "default": "check_and_build"},
        "install_dependencies": {"type": "boolean", "default": False},
        "clean_build": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}


def register(registry: Any) -> None:
    """Register the frontend_build tool on the given ``ToolRegistry``."""
    registry.register(
        "frontend_build",
        toolset="build",
        schema=_SCHEMA,
        handler=_handle_frontend_build,
    )


__all__ = ["_handle_frontend_build", "register"]
