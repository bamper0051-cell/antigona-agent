from __future__ import annotations

from antigona.database import Database
from antigona.models import TaskFlow, TaskState
from antigona.skills import SkillsRegistry


def test_skills_save_load_list() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        registry = SkillsRegistry(session)

        # 1. Save trajectory as skill
        skill = registry.save_skill(
            name="Refactor Python Code",
            trigger="refactor python file",
            trajectory_ref="flow-12345",
            skill_id="skill-001",
        )

        assert skill.id == "skill-001"
        assert skill.name == "Refactor Python Code"
        assert skill.trigger == "refactor python file"
        assert skill.trajectory_ref == "flow-12345"
        assert skill.created_at is not None

        # 2. Load skill
        loaded = registry.get_skill("skill-001")
        assert loaded is not None
        assert loaded.name == "Refactor Python Code"

        # 3. Save from task
        task = TaskFlow(
            id="flow-999",
            owner_id="owner1",
            goal="Test task",
            target_path=".",
            content="",
            payload_fingerprint="fp1",
            idempotency_key="idem1",
            status=TaskState.DONE.value,
        )
        session.add(task)
        session.commit()

        task_skill = registry.save_from_task(
            task_id="flow-999",
            name="Test Task Skill",
            trigger="test trigger",
        )
        assert task_skill.trajectory_ref == "flow-999"

        # 4. List skills
        skills_list = registry.list_skills()
        assert len(skills_list) == 2
        skill_ids = [s.id for s in skills_list]
        assert "skill-001" in skill_ids
        assert task_skill.id in skill_ids
