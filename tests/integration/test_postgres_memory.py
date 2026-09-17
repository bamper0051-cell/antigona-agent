from __future__ import annotations

import os

import pytest

from antigona.memory import MemoryKind, PostgresMemoryStore

pytestmark = pytest.mark.skipif(
    not os.getenv("ANTIGONA_TEST_POSTGRES_URL"),
    reason="ANTIGONA_TEST_POSTGRES_URL is required for real pgvector integration tests",
)


def _vector(*values: float) -> list[float]:
    return [*values, *([0.0] * (1536 - len(values)))]


@pytest.fixture
def store() -> PostgresMemoryStore:
    memory = PostgresMemoryStore(os.environ["ANTIGONA_TEST_POSTGRES_URL"])
    memory.migrate()
    memory.delete_owner("owner-a")
    memory.delete_owner("owner-b")
    return memory


def test_vector_search_is_semantic_owner_isolated_and_persistent(
    store: PostgresMemoryStore,
) -> None:
    first = store.remember(
        owner_id="owner-a",
        content="prefers concise status reports",
        embedding=_vector(1.0, 0.0),
        kind=MemoryKind.SESSION,
        source_ref="flow-1",
    )
    store.remember(
        owner_id="owner-a",
        content="uses Python for automation",
        embedding=_vector(0.0, 1.0),
        kind=MemoryKind.SESSION,
        source_ref="flow-2",
    )
    store.remember(
        owner_id="owner-b",
        content="private memory from another owner",
        embedding=_vector(1.0, 0.0),
        kind=MemoryKind.SESSION,
    )

    reopened = PostgresMemoryStore(os.environ["ANTIGONA_TEST_POSTGRES_URL"])
    matches = reopened.search(owner_id="owner-a", embedding=_vector(0.99, 0.01), limit=2)

    assert matches[0].id == first.id
    assert [match.content for match in matches] == [
        "prefers concise status reports",
        "uses Python for automation",
    ]
    assert all(match.owner_id == "owner-a" for match in matches)
    assert matches[0].similarity > matches[1].similarity


def test_profile_memory_upsert_preserves_single_fact(store: PostgresMemoryStore) -> None:
    original = store.upsert_profile(
        owner_id="owner-a",
        key="response_style",
        content="verbose",
        embedding=_vector(0.0, 1.0),
    )
    updated = store.upsert_profile(
        owner_id="owner-a",
        key="response_style",
        content="concise",
        embedding=_vector(1.0, 0.0),
    )

    assert updated.id == original.id
    assert updated.content == "concise"
    assert updated.kind is MemoryKind.PROFILE
    matches = store.search(
        owner_id="owner-a",
        embedding=_vector(1.0, 0.0),
        kind=MemoryKind.PROFILE,
    )
    assert [(match.profile_key, match.content) for match in matches] == [
        ("response_style", "concise")
    ]


def test_store_rejects_invalid_vectors_without_touching_database(
    store: PostgresMemoryStore,
) -> None:
    with pytest.raises(ValueError, match="1536"):
        store.remember(
            owner_id="owner-a",
            content="bad vector",
            embedding=[1.0, 2.0],
            kind=MemoryKind.SESSION,
        )

    with pytest.raises(ValueError, match="finite"):
        store.search(owner_id="owner-a", embedding=_vector(float("nan")))
