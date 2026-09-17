from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from antigona.models import utcnow


class CriteriaBase(DeclarativeBase):
    """Metadata owned exclusively by the verifier process."""


class _VerifierCriteria(CriteriaBase):
    __tablename__ = "verifier_criteria"

    # Deliberately opaque: there is no ORM relationship or import of TaskFlow here.
    # The SQL migration retains the database-level FK for deployed databases.
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    criteria: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VerifierCriteriaDatabase:
    """Private engine/session boundary initialized only by the verifier service."""

    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        CriteriaBase.metadata.create_all(self.engine)


class MissingVerifierCriteria(LookupError):
    pass


class VerifierCriteriaStore:
    """Verifier-only repository over verifier-owned metadata and sessions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def put(self, task_id: str, criteria: str) -> None:
        if not criteria.strip():
            raise ValueError("verifier criteria must not be empty")
        self.session.merge(_VerifierCriteria(task_id=task_id, criteria=criteria))
        self.session.flush()

    def require(self, task_id: str) -> str:
        row = self.session.get(_VerifierCriteria, task_id)
        if row is None:
            raise MissingVerifierCriteria(task_id)
        return row.criteria