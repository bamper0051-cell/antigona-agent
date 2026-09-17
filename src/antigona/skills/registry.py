from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..durable.state_machine import ConcurrentUpdate
from ..models import Skill, SkillTransition, TaskFlow, utcnow
from .errors import SkillIntegrityError, SkillNotFound
from .lifecycle import (
    VERIFIER_ONLY_SKILL_TRANSITIONS,
    InvalidTransition,
    SkillState,
    check_skill_transition,
)
from .store import CardStore, read_body_safely

__all__ = [
    "ConcurrentUpdate",
    "InvalidTransition",
    "SkillNotFound",
    "SkillsRegistry",
]


def _journal(
    session: Session,
    skill_id: str,
    from_status: str,
    to_status: str,
    actor: str,
    *,
    reason: str | None = None,
    correlation_id: str | None = None,
    accepted: bool = True,
) -> SkillTransition:
    """Append one row to the append-only ``skill_transitions`` table.

    Returns the created :class:`SkillTransition` row.
    """
    transition = SkillTransition(
        id=str(uuid.uuid4()),
        skill_id=skill_id,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        reason=reason,
        correlation_id=correlation_id or str(uuid.uuid4()),
        accepted=accepted,
    )
    session.add(transition)
    session.flush()
    return transition


class SkillsRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --- P0 backward-compatible API ---

    def save_skill(
        self,
        name: str,
        trigger: str,
        trajectory_ref: str,
        skill_id: str | None = None,
    ) -> Skill:
        sid = skill_id or str(uuid.uuid4())
        existing = self.session.get(Skill, sid)
        if existing:
            existing.name = name
            existing.trigger = trigger
            existing.trajectory_ref = trajectory_ref
            self.session.commit()
            return existing
        skill = Skill(
            id=sid,
            name=name,
            trigger=trigger,
            trajectory_ref=trajectory_ref,
        )
        self.session.add(skill)
        self.session.commit()
        return skill

    def save_from_task(
        self,
        task_id: str,
        name: str,
        trigger: str,
    ) -> Skill:
        task = self.session.get(TaskFlow, task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        skill = Skill(
            id=str(uuid.uuid4()),
            name=name,
            trigger=trigger,
            trajectory_ref=task.id,
            owner_id=task.owner_id,
            source_flow_id=task.id,
        )
        self.session.add(skill)
        self.session.commit()
        return skill

    def get_skill(self, skill_id: str) -> Skill | None:
        return self.session.get(Skill, skill_id)

    def list_skills(self) -> Sequence[Skill]:
        return self.session.scalars(select(Skill).order_by(Skill.created_at.desc())).all()

    # --- P2.1 extended API ---

    def register_skill(
        self,
        card_body: bytes,
        owner_id: str,
        state_root: str,
        *,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> Skill:
        """Register a skill from a parsed and rendered ``ASKILL/1`` card body.

        The body is stored in the content-addressed store, a new ``Skill`` row
        is created in ``DRAFT`` status, and the creation is journalled.

        Returns the persisted :class:`Skill` row.
        """
        from .canonical import verify_footer
        from .format import parse_card

        # Verify integrity and parse.
        digest, byte_count = verify_footer(card_body)
        card = parse_card(card_body)

        # Store the body.
        store = CardStore(state_root)
        actual_digest = store.write(card_body)
        if actual_digest != digest:
            raise SkillIntegrityError(
                f"body digest mismatch during registration: computed {actual_digest}"
            )

        body_path = str(store.path(digest).relative_to(store.root))

        skill = Skill(
            id=card.skill_id,
            name=card.slug,
            slug=card.slug,
            version=card.version,
            owner_id=owner_id,
            trigger=" ".join(
                " ".join(r.values) for r in card.match
            ) if card.match else card.intent[0][:255] if card.intent else "",
            trajectory_ref=card.origin.flow,
            status=SkillState.DRAFT.value,
            revision=0,
            format_version=card.format_version,
            body_sha256=digest,
            body_bytes=byte_count,
            body_path=body_path,
            trust=card.trust.value,
            risk_ceiling=card.risk.value,
            source_flow_id=card.origin.flow,
        )
        self.session.add(skill)
        self.session.flush()

        _journal(
            self.session,
            skill.id,
            from_status="",
            to_status=SkillState.DRAFT.value,
            actor=actor,
            reason="skill registered via register_skill",
            correlation_id=correlation_id,
            accepted=True,
        )
        self.session.commit()
        return skill

    def transition_to(
        self,
        skill: Skill,
        target_status: str,
        actor: str,
        *,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Skill:
        """Attempt a generic (non-Verifier-only) skill status transition.

        Raises :class:`InvalidTransition` on illegal transitions.
        Raises :class:`ConcurrentUpdate` on CAS failure.
        Returns the updated skill on success.
        """
        current = SkillState(skill.status)
        target = SkillState(target_status)

        # Check for Verifier-only transitions — the generic API must refuse them.
        if (current, target) in VERIFIER_ONLY_SKILL_TRANSITIONS:
            raise InvalidTransition(
                f"transition {current.value} -> {target.value} requires Verifier"
            )

        check_skill_transition(current, target)

        old_revision = skill.revision
        result = self.session.execute(
            update(Skill)
            .where(
                Skill.id == skill.id,
                Skill.revision == old_revision,
            )
            .values(
                status=target.value,
                revision=old_revision + 1,
                updated_at=utcnow(),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise ConcurrentUpdate(
                f"skill {skill.id} CAS failed on revision {old_revision}"
            )

        self.session.flush()
        _journal(
            self.session,
            skill.id,
            from_status=current.value,
            to_status=target.value,
            actor=actor,
            reason=reason or f"transition {current.value} -> {target.value}",
            correlation_id=correlation_id,
            accepted=True,
        )

        self.session.commit()
        self.session.refresh(skill)
        return skill

    def promote(
        self,
        skill_id: str,
        verifier_actor: str,
        *,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Skill:
        """Verifier-only: promote a CANDIDATE skill to ACTIVE.

        This is the only path that can write ``ACTIVE`` status. It performs a
        CAS on the skill's revision and journalles both success and failure.

        Raises :class:`InvalidTransition` if the skill is not CANDIDATE.
        Raises :class:`ConcurrentUpdate` on CAS failure.
        Raises :class:`SkillNotFound` if no skill with the given id exists.
        """
        skill = self.session.get(Skill, skill_id)
        if not skill:
            raise SkillNotFound(f"skill {skill_id} not found")

        if skill.status != SkillState.CANDIDATE.value:
            raise InvalidTransition(
                f"promote requires CANDIDATE status, got {skill.status}"
            )

        # CAS: CANDIDATE -> ACTIVE, revision+1.
        old_revision = skill.revision
        result = self.session.execute(
            update(Skill)
            .where(
                Skill.id == skill.id,
                Skill.status == SkillState.CANDIDATE.value,
                Skill.revision == old_revision,
            )
            .values(
                status=SkillState.ACTIVE.value,
                revision=old_revision + 1,
                verified_at=utcnow(),
                verified_by=verifier_actor,
                updated_at=utcnow(),
            )
        )
        assert isinstance(result, CursorResult)
        if result.rowcount != 1:
            _journal(
                self.session,
                skill.id,
                from_status=skill.status,
                to_status=SkillState.ACTIVE.value,
                actor=verifier_actor,
                reason=reason or "promotion CAS failed (concurrent update)",
                correlation_id=correlation_id,
                accepted=False,
            )
            self.session.commit()
            raise ConcurrentUpdate(
                f"skill {skill_id} promotion CAS failed on revision {old_revision}"
            )

        self.session.flush()
        _journal(
            self.session,
            skill.id,
            from_status=SkillState.CANDIDATE.value,
            to_status=SkillState.ACTIVE.value,
            actor=verifier_actor,
            reason=reason or "promoted to ACTIVE by Verifier",
            correlation_id=correlation_id,
            accepted=True,
        )
        self.session.commit()
        self.session.refresh(skill)
        return skill

    def quarantine(
        self,
        skill: Skill,
        actor: str,
        *,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Skill:
        """Move a skill to QUARANTINED (integrity or trust violation).

        QUARANTINED is sticky: no transitions are possible out of it.
        """
        if skill.status == SkillState.QUARANTINED.value:
            return skill  # Already quarantined.

        old_revision = skill.revision
        result = self.session.execute(
            update(Skill)
            .where(
                Skill.id == skill.id,
                Skill.revision == old_revision,
            )
            .values(
                status=SkillState.QUARANTINED.value,
                revision=old_revision + 1,
                updated_at=utcnow(),
            )
        )
        assert isinstance(result, CursorResult)
        if result.rowcount != 1:
            raise ConcurrentUpdate(
                f"skill {skill.id} quarantine CAS failed on revision {old_revision}"
            )

        self.session.flush()
        _journal(
            self.session,
            skill.id,
            from_status=skill.status,
            to_status=SkillState.QUARANTINED.value,
            actor=actor,
            reason=reason or "quarantined",
            correlation_id=correlation_id,
            accepted=True,
        )
        self.session.commit()
        self.session.refresh(skill)
        return skill

    def deprecate(
        self,
        skill_id: str,
        actor: str,
        *,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Skill:
        """Deprecate an ACTIVE skill (moves to DEPRECATED).

        Only ACTIVE skills may be deprecated.
        """
        skill = self.session.get(Skill, skill_id)
        if not skill:
            raise SkillNotFound(f"skill {skill_id} not found")

        return self.transition_to(
            skill,
            SkillState.DEPRECATED.value,
            actor,
            reason=reason or "deprecated",
            correlation_id=correlation_id,
        )

    def get_card_body(self, skill: Skill, state_root: str) -> bytes:
        """Read and verify the card body from the content-addressed store."""
        return read_body_safely(Path(state_root), skill.body_sha256)
