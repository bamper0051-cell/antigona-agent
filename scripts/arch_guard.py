#!/usr/bin/env python3
"""Architecture Guard — запрет новых архитектурных отклонений (ADR-007).

Сканирует ``src/antigona/**/*.py`` на паттерны, которые должны появляться
ТОЛЬКО в каноническом ``core/paths.py``:
- ``Path.home()`` (P1)
- ``expanduser(`` (P2)
- хардкод абсолютного пути проекта ``/root/antigona`` / ``/root/.antigona`` (P3)
- самостоятельное определение ``PROJECT_ROOT`` / ``_PROJECT_ROOT`` (P4)

Также фиксирует consolidation inventory (PR-01): legacy Tool и EventBus
импорты, а также прямые провайдерские SDK-импорты вне adapter-модулей.

``core/paths.py`` — единственное исключение (каноническое определение).

Любое НОВОЕ нарушение (не покрытое baseline) -> exit 1, CI падает.
``scripts/arch_baseline.txt`` — allowlist существующих нарушений, который
ОБЯЗАН сокращаться по мере рефакторинга (ADR-007). Добавлять новые строки в
baseline запрещено: это фиксирует архитектурный долг, а не одобряет его.

Запуск: ``python3 scripts/arch_guard.py [--baseline scripts/arch_baseline.txt]``
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SRC = Path("src/antigona")
TESTS = Path("tests")
CANONICAL = Path("core/paths.py")  # единственное исключение

# (имя, regex) — паттерн, запрещённый вне core/paths.py
# P3 — только реальные Path-конструкции с хардкодом корня (не строки в
#       комментариях/диагностике/хелпе); P2 — expanduser в резолве внутренних
#       путей (пользовательский ввод — не запрещён, покрывается baseline).
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("P1_home", re.compile(r"Path\.home\s*\(")),
    ("P2_expanduser", re.compile(r"\.expanduser\s*\(")),
    ("P3_abs_root_path", re.compile(r"Path\(\s*[\"']/root/antigona")),
    ("P4_project_root_def", re.compile(
        r"(?:_?PROJECT_ROOT)\s*=\s*(?:Path\(\s*[\"']|[\"'])")),
    # P5: чтение config.yaml (альтернативный загрузчик конфигурации — ADR-002).
    ("P5_config_yaml_read", re.compile(
        r"(?:yaml\.(?:safe_)?load|open|Path)\s*\(\s*[\"'][^\"']*config\.ya*ml")),
    # P6: строковый хардкод корня (не Path-конструкция) в бизнес-логике.
    ("P6_string_root", re.compile(r"(?<!Path\()[\"']/root/antigona[\"']")),
    # P7: Tool ABC оставлен только для compatibility migration; доменный код
    # должен зависеть от канонического ToolSpec/handler boundary.
    ("P7_legacy_tool_abc", re.compile(
        r"from\s+antigona\.tools\.contracts\s+import[\s\S]{0,200}?\bTool\b",
    )),
    # P8: task-local EventBus расщепляет runtime/task event delivery; канон —
    # antigona.events.bus.EventBus до завершения compatibility migration.
    ("P8_legacy_event_bus", re.compile(
        r"from\s+antigona\.tasks\.event_bus\s+import\b",
    )),
    # P9: provider SDK calls должны быть изолированы в adapter-модулях, чтобы
    # доменный слой не зависел от конкретного LLM vendor SDK.
    ("P9_direct_provider_sdk", re.compile(
        r"(?:from\s+(?:openai|anthropic|google\.generativeai|deepseek)\b|"
        r"import\s+(?:openai|anthropic|google\.generativeai|deepseek)\b)",
    )),
]

# ── Portability guard (ADR-007 / regression guard) ────────────────────────────
# Запрещает появление серверно-специфичных абсолютных путей в ЛЮБОМ коде и
# тестах (src + tests). Clean clone не должен зависеть от соседних репозиториев
# или домашней директории конкретного пользователя.
#   P7: legacy server repo `/root/antigona-cli` — путь, ломавший collection.
#   P8: абсолютный путь в домашней директории пользователя `/home/<user>/`.
PORTABILITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("P7_cli_repo", re.compile(r"/root/antigona-cli")),
    ("P8_home_abs", re.compile(r"/home/[A-Za-z0-9_.-]+/")),
]

# Явные исключения (repo-root-relative path) с обоснованием. Добавлять новые
# строки только когда файл ЗАКОННО ссылается на запрещённый путь (guard-fixture,
# документирующий запрет).
PORTABILITY_ALLOW: dict[str, str] = {
    "tests/packaging/test_unified_package_contract.py": (
        "guard-тест: ассертит ОТСУТСТВИЕ '/root/antigona-cli' в cli.py"
    ),
    "src/antigona/datasets/corrections.py": (
        "'/home/user' — пример из разговорных данных (диалог), не filesystem-путь"
    ),
}

SKIP_SUFFIXES = ("__pycache__",)
ADAPTER_PATH_PART = "/adapters/"


def is_adapter_module(path: Path) -> bool:
    """Return whether *path* is an explicit provider/compatibility adapter."""
    return ADAPTER_PATH_PART in f"/{path.as_posix()}"


def scan_text(text: str, path: Path) -> list[str]:
    """Return architecture pattern names found in one source file."""
    if is_adapter_module(path):
        return [name for name, rx in PATTERNS[:6] if rx.search(text)]
    return [name for name, rx in PATTERNS if rx.search(text)]


def scan() -> list[tuple[Path, str]]:
    hits: list[tuple[Path, str]] = []
    for py in SRC.rglob("*.py"):
        if any(s in str(py) for s in SKIP_SUFFIXES):
            continue
        if py.relative_to(SRC) == CANONICAL:
            continue  # core/paths.py — канон
        text = py.read_text(encoding="utf-8")
        for name in scan_text(text, py):
            hits.append((py, name))
    return hits


def scan_portability() -> list[tuple[str, str]]:
    """Сканирует src + tests на серверно-специфичные абсолютные пути.

    Возвращает пары (repo-root-relative path, pattern name) для НОВЫХ
    нарушений (не покрытых явным PORTABILITY_ALLOW).
    """
    hits: list[tuple[str, str]] = []
    for root in (SRC, TESTS):
        for py in root.rglob("*.py"):
            if any(s in str(py) for s in SKIP_SUFFIXES):
                continue
            rel = py.as_posix()
            if rel in PORTABILITY_ALLOW:
                continue
            text = py.read_text(encoding="utf-8")
            for name, rx in PORTABILITY_PATTERNS:
                if rx.search(text):
                    hits.append((rel, name))
    return hits


def load_baseline(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="scripts/arch_baseline.txt")
    args = ap.parse_args()

    hits = scan()
    baseline = load_baseline(Path(args.baseline))

    def key(h: tuple[Path, str]) -> str:
        return f"{h[0].relative_to(SRC).as_posix()}:{h[1]}"

    new_violations = [h for h in hits if key(h) not in baseline]
    covered = [h for h in hits if key(h) in baseline]
    port_hits = scan_portability()

    print(f"Architecture Guard: {len(hits)} паттерн(ов), "
          f"{len(covered)} покрыто baseline, {len(new_violations)} новых, "
          f"{len(port_hits)} portability-нарушений.")
    for py, name in sorted(hits):
        marker = "BASELINE " if key((py, name)) in baseline else "NEW      "
        print(f"  [{marker}] {name}  {py.as_posix()}")
    for rel, name in sorted(port_hits):
        print(f"  [PORT     ] {name}  {rel}")

    if new_violations:
        print("\n⛔ НОВЫЕ архитектурные нарушения (не в baseline). "
              "Используйте Unified Paths API (core/paths.py).")
        return 1
    if port_hits:
        print("\n⛔ Portability-нарушения: серверно-специфичные абсолютные пути "
              "в src/tests. Clean clone не должен зависеть от них.")
        return 1
    print("\n✅ Architecture Guard: новых нарушений нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
