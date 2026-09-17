from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from sqlalchemy import inspect as sqlalchemy_inspect

from antigona.database import Base, Database
from antigona.models import TaskFlow


def test_shared_orm_and_component_imports_expose_no_verifier_criteria() -> None:
    models = importlib.import_module("antigona.models")
    worker = importlib.import_module("antigona.worker")
    gateway = importlib.import_module("antigona.gateway")

    assert not hasattr(models, "VerifierCriteria")
    assert not hasattr(TaskFlow, "verifier_criteria")
    assert "verifier_criteria" not in Base.metadata.tables
    assert "verifier_criteria" not in sqlalchemy_inspect(TaskFlow).relationships
    for module in (worker, gateway):
        assert not any("criteria" in name.lower() for name in vars(module))


def test_production_judge_contains_no_fake_provider_compatibility_path() -> None:
    judge = importlib.import_module("antigona.verifier.judge")
    source = inspect.getsource(judge)

    assert "mock_evaluator" not in source
    assert "_ExplicitFakeProvider" not in source
    assert not any("fake" in name.lower() for name in vars(judge))


def test_verifier_private_store_has_separate_metadata_session_and_is_idempotent(tmp_path: Path) -> None:
    from antigona.verifier.criteria import (
        CriteriaBase,
        VerifierCriteriaDatabase,
        VerifierCriteriaStore,
    )

    url = f"sqlite:///{tmp_path / 'private.db'}"
    shared = Database(url)
    shared.create_all()
    private = VerifierCriteriaDatabase(url)
    private.create_all()

    assert CriteriaBase.metadata is not Base.metadata
    assert "verifier_criteria" not in Base.metadata.tables
    assert "verifier_criteria" in CriteriaBase.metadata.tables
    assert private.session_factory is not shared.session_factory

    with private.session_factory() as session:
        store = VerifierCriteriaStore(session)
        store.put("opaque-task-id", "first")
        store.put("opaque-task-id", "updated")
        session.commit()

    with private.session_factory() as session:
        assert VerifierCriteriaStore(session).require("opaque-task-id") == "updated"
