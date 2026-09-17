from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Artifact, FlowStep, StateTransition, TaskFlow

__all__ = [
    "ReplayEngine",
    "ReplayTaskNotFound",
    "ReplayTrajectory",
    "StepReplay",
    "TransitionReplay",
    "ReplayTimelineEntry",
]


class ReplayTaskNotFound(Exception):
    pass


@dataclass
class StepReplay:
    id: str
    index: int
    title: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    retries: int


@dataclass
class TransitionReplay:
    id: int
    entity_id: str
    entity_type: str
    from_state: str | None
    to_state: str
    reason: str
    actor: str
    created_at: str


@dataclass
class ReplayTimelineEntry:
    type: str
    timestamp: str
    entity_id: str
    description: str
    details: dict[str, Any] | None = None


@dataclass
class ReplayTrajectory:
    task_id: str
    owner_id: str
    goal: str
    target_path: str
    status: str
    revision: int
    created_at: str
    updated_at: str
    steps: list[StepReplay]
    transitions: list[TransitionReplay]
    artifacts: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReplayEngine:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_trajectory(
        self,
        task_id: str,
        owner_id: str | None = None,
        actor: str | None = None,
        entity_type: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> ReplayTrajectory:
        flow = self.session.get(TaskFlow, task_id)
        if not flow:
            raise ReplayTaskNotFound(f"TaskFlow {task_id} not found")

        if owner_id is not None and flow.owner_id != owner_id:
            raise ReplayTaskNotFound(f"TaskFlow {task_id} not found")

        raw_steps = self.session.scalars(
            select(FlowStep).where(FlowStep.task_id == task_id).order_by(FlowStep.index)
        ).all()
        steps = [
            StepReplay(
                id=s.id,
                index=s.index,
                title=s.title,
                status=s.status,
                input=s.input,
                output=s.output,
                retries=s.retries,
            )
            for s in raw_steps
        ]

        trans_query = (
            select(StateTransition)
            .where(StateTransition.task_id == task_id)
            .order_by(StateTransition.id)
        )
        if actor is not None:
            trans_query = trans_query.where(StateTransition.actor == actor)
        if entity_type is not None:
            trans_query = trans_query.where(StateTransition.entity_type == entity_type)
        if from_dt is not None:
            trans_query = trans_query.where(StateTransition.created_at >= from_dt)
        if to_dt is not None:
            trans_query = trans_query.where(StateTransition.created_at <= to_dt)

        raw_trans = self.session.scalars(trans_query).all()
        transitions = [
            TransitionReplay(
                id=t.id,
                entity_id=t.entity_id,
                entity_type=t.entity_type,
                from_state=t.from_state,
                to_state=t.to_state,
                reason=t.reason,
                actor=t.actor,
                created_at=t.created_at.isoformat(),
            )
            for t in raw_trans
        ]

        raw_art = self.session.scalars(
            select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.created_at)
        ).all()
        artifacts = [
            {
                "id": a.id,
                "step_id": a.step_id,
                "path": a.path,
                "sha256": a.sha256,
                "size": a.size,
                "verified": a.verified,
            }
            for a in raw_art
        ]

        return ReplayTrajectory(
            task_id=flow.id,
            owner_id=flow.owner_id,
            goal=flow.goal,
            target_path=flow.target_path,
            status=flow.status,
            revision=flow.revision,
            created_at=flow.created_at.isoformat(),
            updated_at=flow.updated_at.isoformat(),
            steps=steps,
            transitions=transitions,
            artifacts=artifacts,
        )

    def get_rejected_transitions(
        self,
        task_id: str,
        owner_id: str | None = None,
    ) -> list[TransitionReplay]:
        flow = self.session.get(TaskFlow, task_id)
        if not flow:
            raise ReplayTaskNotFound(f"TaskFlow {task_id} not found")
        if owner_id is not None and flow.owner_id != owner_id:
            raise ReplayTaskNotFound(f"TaskFlow {task_id} not found")

        raw = self.session.scalars(
            select(StateTransition)
            .where(
                StateTransition.task_id == task_id,
                StateTransition.from_state == StateTransition.to_state,
            )
            .order_by(StateTransition.id)
        ).all()
        return [
            TransitionReplay(
                id=t.id,
                entity_id=t.entity_id,
                entity_type=t.entity_type,
                from_state=t.from_state,
                to_state=t.to_state,
                reason=t.reason,
                actor=t.actor,
                created_at=t.created_at.isoformat(),
            )
            for t in raw
        ]

    def get_timeline(
        self,
        task_id: str,
        owner_id: str | None = None,
    ) -> list[ReplayTimelineEntry]:
        """Flat chronological merge of transitions, steps and artifacts."""
        # First verify access
        flow = self.session.get(TaskFlow, task_id)
        if not flow:
            raise ReplayTaskNotFound(f"TaskFlow {task_id} not found")
        if owner_id is not None and flow.owner_id != owner_id:
            raise ReplayTaskNotFound(f"TaskFlow {task_id} not found")

        entries: list[ReplayTimelineEntry] = []

        # Transitions
        raw_trans = self.session.scalars(
            select(StateTransition)
            .where(StateTransition.task_id == task_id)
            .order_by(StateTransition.id)
        ).all()
        for t in raw_trans:
            desc = f"{t.from_state or 'NONE'} → {t.to_state}"
            if t.from_state == t.to_state:
                entry_type = "rejected"
            else:
                entry_type = "transition"
            entries.append(
                ReplayTimelineEntry(
                    type=entry_type,
                    timestamp=t.created_at.isoformat(),
                    entity_id=t.entity_id,
                    description=desc,
                    details={
                        "actor": t.actor,
                        "reason": t.reason,
                        "entity_type": t.entity_type,
                    },
                )
            )

        # Steps don't have individual created_at; transitions already cover them.

        # Artifacts
        raw_art = self.session.scalars(
            select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.created_at)
        ).all()
        for a in raw_art:
            entries.append(
                ReplayTimelineEntry(
                    type="artifact",
                    timestamp=a.created_at.isoformat(),
                    entity_id=a.id,
                    description=f"{a.path} ({a.size}B, sha256:{a.sha256[:8]}...)",
                    details={
                        "verified": a.verified,
                        "step_id": a.step_id,
                    },
                )
            )

        # Sort by timestamp
        entries.sort(key=lambda e: e.timestamp)
        return entries

    def render_replay_text(
        self,
        task_id: str,
        actor: str | None = None,
        entity_type: str | None = None,
    ) -> str:
        trajectory = self.get_trajectory(
            task_id, actor=actor, entity_type=entity_type
        )
        return self._format_rich(trajectory)

    def render_timeline_text(self, task_id: str, owner_id: str | None = None) -> str:
        entries = self.get_timeline(task_id, owner_id=owner_id)
        import io

        from rich.console import Console as RichConsole

        buf = io.StringIO()
        console = RichConsole(file=buf, width=120)

        console.print(f"[bold]Timeline for {task_id}:[/bold]")
        for entry in entries:
            style = {
                "transition": "cyan",
                "rejected": "yellow",
                "artifact": "green",
            }.get(entry.type, "white")
            console.print(
                f"  {entry.timestamp}  [{style}][{entry.type}][/]  {entry.description}"
            )
        return buf.getvalue()

    def _format_rich(self, trajectory: ReplayTrajectory) -> str:
        import io

        from rich.console import Console as RichConsole

        buf = io.StringIO()
        console = RichConsole(file=buf, width=120)

        # ── Summary Panel ──────────────────────────────────────────────
        status_color = {
            "DONE": "green",
            "FAILED": "red",
            "CANCELLED": "grey",
            "TIMEOUT": "red",
            "BLOCKED": "yellow",
            "POLICY_DENIED": "red",
        }.get(trajectory.status, "white")

        summary = (
            f"Goal: {trajectory.goal}\n"
            f"Status: [{status_color}]{trajectory.status}[/]     Revision: {trajectory.revision}\n"
            f"Owner: {trajectory.owner_id}     Path: {trajectory.target_path}\n"
            f"Created: {trajectory.created_at}   Updated: {trajectory.updated_at}"
        )
        console.print(
            Panel(
                summary,
                title=f"[bold]Flow Replay: {trajectory.task_id}[/bold]",
                border_style="blue",
            )
        )

        # ── Transitions Table ──────────────────────────────────────────
        if trajectory.transitions:
            table = Table(title="State Transitions")
            table.add_column("#", style="dim")
            table.add_column("Entity", style="cyan")
            table.add_column("From", style="yellow")
            table.add_column("To", style="green")
            table.add_column("Reason", style="white")
            table.add_column("Actor", style="magenta")

            for tr in trajectory.transitions:
                is_rejected = tr.from_state == tr.to_state
                row_style = "yellow" if is_rejected else ""
                table.add_row(
                    str(tr.id),
                    f"{tr.entity_type} ({tr.entity_id[:8]}...)",
                    tr.from_state or "NONE",
                    Text(tr.to_state, style="green" if not is_rejected else "yellow"),
                    tr.reason[:60] + ("..." if len(tr.reason) > 60 else ""),
                    tr.actor,
                    style=row_style,
                )
            console.print(table)
        else:
            console.print("[dim](No transitions recorded)[/dim]")

        # ── Steps Tree ─────────────────────────────────────────────────
        if trajectory.steps:
            tree = Tree("[bold]Flow Steps[/bold]")
            for step in trajectory.steps:
                status_style = {
                    "COMPLETED": "green",
                    "FAILED": "red",
                    "CANCELLED": "grey",
                    "RUNNING": "cyan",
                    "PENDING": "yellow",
                }.get(step.status, "white")
                branch = tree.add(
                    f"Step #{step.index}: {step.title} "
                    f"[{status_style}]{step.status}[/] (retries={step.retries})"
                )
                branch.add(f"Input:  {step.input}")
                if step.output is not None:
                    branch.add(f"Output: {step.output}")
            console.print(tree)

        # ── Artifacts Table ────────────────────────────────────────────
        if trajectory.artifacts:
            art_table = Table(title="Artifacts")
            art_table.add_column("ID", style="dim")
            art_table.add_column("Path", style="cyan")
            art_table.add_column("SHA256", style="green")
            art_table.add_column("Size", style="white")
            art_table.add_column("Verified", style="bold")

            for a in trajectory.artifacts:
                verified_text = Text("✅", style="green") if a["verified"] else Text("❌", style="red")
                art_table.add_row(
                    a["id"][:12] + "...",
                    a["path"],
                    a["sha256"][:12] + "...",
                    f"{a['size']:,} B",
                    verified_text,
                )
            console.print(art_table)

        # ── Rejected transitions summary ────────────────────────────────
        rejected = [t for t in trajectory.transitions if t.from_state == t.to_state]
        if rejected:
            console.print(f"\n[bold yellow]REJECTED transitions: {len(rejected)}[/bold yellow]")
            for r in rejected:
                console.print(f"  [{r.id}] {r.from_state} → {r.to_state} rejected by {r.actor}")

        return buf.getvalue()

    def to_json(self, trajectory: ReplayTrajectory) -> str:
        import json

        return json.dumps(trajectory.to_dict(), indent=2, ensure_ascii=False)
