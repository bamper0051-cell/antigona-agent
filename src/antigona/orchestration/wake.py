from datetime import datetime

from .models import WakeEvent
from .state import GOAL_TERMINAL, GoalState, WakeKind, WakeStatus
from .store import GoalTransitionError, OrchestrationStore, is_timer_expired


class WakeManager:
    def __init__(self, orch: OrchestrationStore) -> None:
        self.orch = orch

    def process_pending(self, now: datetime | None = None) -> int:
        """Process all PENDING wake events and expired WAITING-goal timers.

        Returns the number of goals resumed to ACTIVE.
        For each pending WakeEvent (orch.pending_wakes()):
          - goal = orch.get_goal(ev.goal_id); if goal is None or terminal -> mark IGNORED.
          - if goal.status == WAITING: orch.transition_goal(goal.id, WAITING, ACTIVE,
              reason=f"wake:{ev.kind}") and orch.mark_wake(ev.id, FIRED).
          - if goal.status == BLOCKED and ev.kind in (SERVICE_AVAILABLE, MANUAL_RESUME,
              TIMER_EXPIRED): transition BLOCKED -> ACTIVE, mark FIRED.
          - else: mark_wake(ev.id, IGNORED) (event for a non-waiting goal is noise).
        Then scan orch.list_goals(status='WAITING'): if is_timer_expired(goal):
          transition WAITING -> ACTIVE reason='timer expired'.
        Any GoalTransitionError from a CAS race (another engine resumed it first)
        must be caught and the wake marked IGNORED (not crash the loop).
        """
        resumed_count = 0

        ev: WakeEvent
        for ev in self.orch.pending_wakes():

            if not ev.goal_id:
                self.orch.mark_wake(ev.id, WakeStatus.IGNORED)
                continue

            goal = self.orch.get_goal(ev.goal_id)
            if goal is None or GoalState(goal.status) in GOAL_TERMINAL:
                self.orch.mark_wake(ev.id, WakeStatus.IGNORED)
                continue

            goal_status = GoalState(goal.status)
            ev_kind = (
                WakeKind(ev.kind)
                if isinstance(ev.kind, str) and ev.kind in WakeKind._value2member_map_
                else ev.kind
            )

            if goal_status == GoalState.WAITING:
                try:
                    self.orch.transition_goal(
                        goal.id,
                        GoalState.WAITING,
                        GoalState.ACTIVE,
                        reason=f"wake:{ev.kind}",
                    )
                    self.orch.mark_wake(ev.id, WakeStatus.FIRED)
                    resumed_count += 1
                except GoalTransitionError:
                    self.orch.mark_wake(ev.id, WakeStatus.IGNORED)
            elif (
                goal_status == GoalState.BLOCKED
                and ev_kind in (
                    WakeKind.SERVICE_AVAILABLE,
                    WakeKind.MANUAL_RESUME,
                    WakeKind.TIMER_EXPIRED,
                    WakeKind.SERVICE_AVAILABLE.value,
                    WakeKind.MANUAL_RESUME.value,
                    WakeKind.TIMER_EXPIRED.value,
                )
            ):
                try:
                    self.orch.transition_goal(
                        goal.id,
                        GoalState.BLOCKED,
                        GoalState.ACTIVE,
                        reason=f"wake:{ev.kind}",
                    )
                    self.orch.mark_wake(ev.id, WakeStatus.FIRED)
                    resumed_count += 1
                except GoalTransitionError:
                    self.orch.mark_wake(ev.id, WakeStatus.IGNORED)
            else:
                self.orch.mark_wake(ev.id, WakeStatus.IGNORED)

        for goal in self.orch.list_goals(status=GoalState.WAITING.value):
            if is_timer_expired(goal, now=now):
                try:
                    self.orch.transition_goal(
                        goal.id,
                        GoalState.WAITING,
                        GoalState.ACTIVE,
                        reason="timer expired",
                    )
                    resumed_count += 1
                except GoalTransitionError:
                    pass

        return resumed_count

    def wait(
        self,
        goal_id: str,
        *,
        reason: str,
        wake_at_iso: str | None = None,
        wake_kind: WakeKind | str | None = None,
    ) -> None:
        """Put a goal into WAITING durably: orch.update_meta(goal_id, wake_at=wake_at_iso,
        wait_reason=reason); orch.transition_goal(goal_id, ACTIVE, WAITING, reason=reason).
        If wake_kind is given, orch.enqueue_wake(wake_kind, goal_id=goal_id,
        payload={"reason": reason})."""
        self.orch.update_meta(goal_id, wake_at=wake_at_iso, wait_reason=reason)
        self.orch.transition_goal(
            goal_id, GoalState.ACTIVE, GoalState.WAITING, reason=reason
        )
        if wake_kind is not None:
            self.orch.enqueue_wake(wake_kind, goal_id=goal_id, payload={"reason": reason})
