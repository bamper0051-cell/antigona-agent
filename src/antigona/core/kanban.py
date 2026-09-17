"""File-based Kanban board (kanban-md inspired).

A lightweight, agents-first Kanban backed by plain files — no DB, no server,
just a board directory. Cards are small JSON files in column subdirectories
(``todo/``, ``in-progress/``, ``review/``, ``done/``). A ``claim`` field on
each card lets multiple agents/humans coordinate without clashing (the same
idea as kanban-md's claim primitive).

Board layout::

    <board_root>/
      todo/<id>.json
      in-progress/<id>.json
      review/<id>.json
      done/<id>.json

Usage::

    from antigona.core.kanban import KanbanBoard
    board = KanbanBoard(str(kanban_dir()))  # antigona.core.paths.kanban_dir()
    board.create(title="Implement X")
    board.claim("x", agent="worker-1")
    board.move("x", "in-progress")
    board.done("x")
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "KanbanBoard",
    "COLUMNS",
    "DEFAULT_COLUMNS",
]

# column name -> subdirectory name
COLUMNS = {
    "todo": "todo",
    "in-progress": "in-progress",
    "review": "review",
    "done": "done",
}
DEFAULT_COLUMNS = list(COLUMNS.keys())


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    return out or "task"


class KanbanBoard:
    """File-backed Kanban board with claim-based coordination."""

    def __init__(self, board_root: str | Path) -> None:
        self.root = Path(board_root).resolve()
        for col in COLUMNS.values():
            (self.root / col).mkdir(parents=True, exist_ok=True)

    # ── low-level helpers ──────────────────────────────────────────────────
    def _paths(self, card_id: str) -> list[Path]:
        return [(self.root / col / f"{card_id}.json") for col in COLUMNS.values()]

    def _find(self, card_id: str) -> tuple[str, Path] | None:
        for col, path in zip(COLUMNS.values(), self._paths(card_id), strict=True):
            if path.exists():
                return col, path
        return None

    # ── create / read ──────────────────────────────────────────────────────
    def create(self, title: str, body: str = "", agent: str = "") -> str:
        card_id = _slug(title) + "-" + uuid.uuid4().hex[:6]
        card = {
            "id": card_id,
            "title": title,
            "body": body,
            "column": "todo",
            "claim": agent or None,
            "claimed_at": time.time() if agent else None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        path = self.root / COLUMNS["todo"] / f"{card_id}.json"
        path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        return card_id

    def get(self, card_id: str) -> dict[str, Any] | None:
        found = self._find(card_id)
        if not found:
            return None
        return dict(json.loads(found[1].read_text(encoding="utf-8")))

    def list(self, column: str | None = None) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for col in COLUMNS.values():
            if column and col != COLUMNS[column]:
                continue
            for p in sorted((self.root / col).glob("*.json")):
                cards.append(json.loads(p.read_text(encoding="utf-8")))
        return cards

    # ── coordination ───────────────────────────────────────────────────────
    def claim(self, card_id: str, agent: str, force: bool = False) -> bool:
        card = self.get(card_id)
        if card is None:
            return False
        if card.get("claim") and not force:
            return False  # already claimed by another agent
        card["claim"] = agent
        card["claimed_at"] = time.time()
        card["updated_at"] = time.time()
        self._write(card_id, card)
        return True

    def release(self, card_id: str, agent: str | None = None) -> bool:
        card = self.get(card_id)
        if card is None:
            return False
        if agent and card.get("claim") != agent:
            return False  # don't release someone else's claim
        card["claim"] = None
        card["claimed_at"] = None
        card["updated_at"] = time.time()
        self._write(card_id, card)
        return True

    # ── transitions ────────────────────────────────────────────────────────
    def move(self, card_id: str, column: str) -> bool:
        if column not in COLUMNS:
            raise ValueError(f"unknown column: {column}")
        found = self._find(card_id)
        if not found:
            return False
        _old_col, path = found
        card = json.loads(path.read_text(encoding="utf-8"))
        card["column"] = column
        card["updated_at"] = time.time()
        dest = self.root / COLUMNS[column] / f"{card_id}.json"
        dest.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        path.unlink(missing_ok=True)
        return True

    def done(self, card_id: str) -> bool:
        return self.move(card_id, "done")

    def _write(self, card_id: str, card: dict[str, Any]) -> None:
        found = self._find(card_id)
        if not found:
            return
        (self.root / COLUMNS[card["column"]] / f"{card_id}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
