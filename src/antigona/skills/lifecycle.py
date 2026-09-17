"""Skill lifecycle state machine.

Structural mirror of :mod:`antigona.durable.state_machine`: the transition table below
is the single authority for skill status changes, and ``CANDIDATE -> ACTIVE`` is
deliberately absent from it. Promotion is a Verifier-only capability (see
:data:`VERIFIER_ONLY_SKILL_TRANSITIONS`) guarded out of band by a bearer-checked,
revision-CAS endpoint — exactly as ``VERIFYING -> DONE`` is for task flows. Neither
Gateway nor Worker can reach ``ACTIVE`` through this graph.

``QUARANTINED`` is reachable from every other state and absorbing: an integrity or
trust violation is sticky by construction.

Journalling of every attempt — accepted and refused alike — into ``skill_transitions``
lands in P2.1.g together with the CAS update; this module only decides legality.
"""

from __future__ import annotations

from enum import StrEnum

from ..durable.state_machine import InvalidTransition

__all__ = [
    "SKILL_TRANSITIONS",
    "TERMINAL_SKILL_STATES",
    "VERIFIER_ONLY_SKILL_TRANSITIONS",
    "InvalidTransition",
    "SkillState",
    "check_skill_transition",
]


class SkillState(StrEnum):
    DRAFT = "DRAFT"
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"


#: Absorbing states. Quarantine has no way out — a poisoned card stays poisoned.
TERMINAL_SKILL_STATES: frozenset[SkillState] = frozenset({SkillState.QUARANTINED})

#: Legal skill transitions reachable through the generic (registry) API.
SKILL_TRANSITIONS: dict[SkillState, frozenset[SkillState]] = {
    SkillState.DRAFT: frozenset(
        {SkillState.CANDIDATE, SkillState.REJECTED, SkillState.QUARANTINED}
    ),
    SkillState.CANDIDATE: frozenset({SkillState.REJECTED, SkillState.QUARANTINED}),
    SkillState.ACTIVE: frozenset({SkillState.DEPRECATED, SkillState.QUARANTINED}),
    SkillState.DEPRECATED: frozenset({SkillState.QUARANTINED}),
    SkillState.REJECTED: frozenset({SkillState.QUARANTINED}),
}

#: Transitions only the Verifier capability may perform, never the generic API.
VERIFIER_ONLY_SKILL_TRANSITIONS: frozenset[tuple[SkillState, SkillState]] = frozenset(
    {
        (SkillState.CANDIDATE, SkillState.ACTIVE),
        (SkillState.DRAFT, SkillState.ACTIVE),
    }
)


def check_skill_transition(current: SkillState, target: SkillState) -> None:
    """Raise :class:`InvalidTransition` unless ``current -> target`` is legal.

    Terminal states are absorbing and unknown edges are forbidden. ``CANDIDATE ->
    ACTIVE`` is refused here even for a Verifier caller: the promotion path does not
    go through the generic graph.
    """
    if current in TERMINAL_SKILL_STATES or target not in SKILL_TRANSITIONS.get(
        current, frozenset()
    ):
        raise InvalidTransition(f"skill {current.value} -> {target.value} is forbidden")
