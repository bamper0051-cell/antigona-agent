from __future__ import annotations

from dataclasses import dataclass

from antigona.database import Database
from antigona.repository import TaskRepository
from antigona.verifier import (
    LLMJudge,
    ProviderResult,
    VerifierCriteriaDatabase,
    VerifierCriteriaStore,
)
from antigona.verifier.judge import JudgeRequest

PRIMARY_TEST_MODEL = "test-primary-model"
VERIFIER_TEST_MODEL = "test-independent-verifier-model"


@dataclass(frozen=True)
class DeterministicVerifierProvider:
    """Explicit, network-free provider implementing the P1.2 result contract."""

    approved: bool | None = None
    reason: str = "deterministic private-criteria evaluation"

    def evaluate(self, request: JudgeRequest, *, model: str) -> ProviderResult:
        approved = self.approved
        if approved is None:
            approved = request.actual_content == request.criteria
        return ProviderResult(approved, self.reason, model)


def deterministic_test_judge(*, approved: bool | None = None) -> LLMJudge:
    return LLMJudge(
        primary_model=PRIMARY_TEST_MODEL,
        verifier_model=VERIFIER_TEST_MODEL,
        provider=DeterministicVerifierProvider(approved=approved),
    )


def seed_private_criteria(database_url: str, task_id: str, criteria: str | None = None) -> None:
    """Seed criteria through the verifier-private store, never worker-visible APIs."""

    database = Database(database_url)
    with database.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        private_criteria = task.content if criteria is None else criteria
    criteria_database = VerifierCriteriaDatabase(database_url)
    criteria_database.create_all()
    with criteria_database.session_factory() as session:
        VerifierCriteriaStore(session).put(task_id, private_criteria)
        session.commit()
