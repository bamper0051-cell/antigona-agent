"""Tests for antigona.core.kanban."""

from __future__ import annotations

import pytest

from antigona.core.kanban import KanbanBoard


@pytest.fixture
def board(tmp_path):
    return KanbanBoard(tmp_path / "board")


def test_create_and_get(board):
    cid = board.create(title="Implement X", body="details", agent="worker-1")
    card = board.get(cid)
    assert card is not None
    assert card["title"] == "Implement X"
    assert card["column"] == "todo"
    assert card["claim"] == "worker-1"


def test_claim_coordination(board):
    cid = board.create(title="Shared task")
    assert board.claim(cid, "alice") is True
    assert board.claim(cid, "bob") is False  # already claimed
    assert board.release(cid, "alice") is True
    assert board.claim(cid, "bob") is True  # freed


def test_release_wrong_agent_blocked(board):
    cid = board.create(title="Task")
    board.claim(cid, "alice")
    assert board.release(cid, "bob") is False  # bob can't release alice's claim
    assert board.release(cid, "alice") is True


def test_move_columns(board):
    cid = board.create(title="Flow")
    assert board.move(cid, "in-progress") is True
    assert board.get(cid)["column"] == "in-progress"
    assert board.move(cid, "review") is True
    assert board.move(cid, "done") is True
    assert board.get(cid)["column"] == "done"


def test_done_helper(board):
    cid = board.create(title="Finish me")
    assert board.done(cid) is True
    assert board.get(cid)["column"] == "done"


def test_list_by_column(board):
    a = board.create(title="A")
    b = board.create(title="B")
    board.move(b, "done")
    todo = board.list(column="todo")
    done = board.list(column="done")
    assert len(todo) == 1 and todo[0]["id"] == a
    assert len(done) == 1 and done[0]["id"] == b


def test_unknown_column_raises(board):
    cid = board.create(title="X")
    with pytest.raises(ValueError):
        board.move(cid, "nope")


def test_missing_card(board):
    assert board.get("does-not-exist") is None
    assert board.move("does-not-exist", "done") is False
