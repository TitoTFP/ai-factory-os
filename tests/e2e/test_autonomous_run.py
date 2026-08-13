from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, Artifact, Event, Factory, FactoryRun, Goal, Message, Space, Task, Tool
from app.services import Runtime, execute_tool
from app.security import utc_now


def test_autonomous_run_evaluates_criteria_reorganizes_and_recovers(client, auth, monkeypatch):
    created = client.post(
        "/api/factories",
        headers=auth,
        json={
            "name": "Autonomous Factory",
            "mission": "Ship evidence",
            "primary_objective": "Create a verified artifact",
            "provider_api_key": "secret",
            "tool_permissions": ["workspace"],
        },
    )
    assert created.status_code == 201
    factory_id = created.json()["id"]
    plan = json.dumps({
        "spaces": [{"name": "Build", "purpose": "Build evidence"}, {"name": "Review", "purpose": "Review evidence"}],
        "agents": [
            {"name": "Builder", "role": "Builder", "space": "Build", "objective": "Create artifact", "responsibilities": ["build"]},
            {"name": "Analyst", "role": "Analyst", "space": "Build", "objective": "Analyze artifact", "responsibilities": ["analyze"]},
            {"name": "Reviewer", "role": "Reviewer", "space": "Review", "objective": "Review artifact", "responsibilities": ["review"]},
        ],
        "goals": [{"title": "Evidence", "objective": "Create verified artifact", "criteria": ["verified artifact"]}],
    })

    async def fake_chat(self, messages, *, json_mode=False):
        return plan if json_mode else "verified artifact"

    monkeypatch.setattr("app.services.OpenAICompatibleProvider.chat", fake_chat)
    assert client.post(f"/api/factories/{factory_id}/architect", headers=auth).status_code == 200
    assert client.post(f"/api/factories/{factory_id}/run", headers=auth).status_code == 200

    db = SessionLocal()
    try:
        goal = db.scalar(select(Goal).where(Goal.factory_id == factory_id))
        agent = db.scalar(select(Agent).where(Agent.factory_id == factory_id))
        task = db.scalar(select(Task).where(Task.factory_id == factory_id))
        assert goal and agent and task
        task.description = "verified artifact"
        db.commit()
        runtime = Runtime()
        asyncio.run(runtime.process_factory(factory_id))
        asyncio.run(runtime.process_factory(factory_id))
        db.expire_all()
        completed_task = db.get(Task, task.id)
        completed_goal = db.get(Goal, goal.id)
        assert completed_task is not None and completed_task.status == "done"
        assert completed_goal is not None and completed_goal.status == "completed"
        assert db.scalar(select(Message).where(Message.factory_id == factory_id, Message.message_type == "TASK_RESULT"))
        assert db.scalar(select(Artifact).where(Artifact.factory_id == factory_id))
        assert db.scalar(select(Event).where(Event.factory_id == factory_id, Event.event_type == "goal_evaluated"))
        assert db.scalar(select(Tool).where(Tool.factory_id == factory_id, Tool.name == "workspace"))
        workspace_tool = db.scalar(select(Tool).where(Tool.factory_id == factory_id, Tool.name == "workspace"))
        assert workspace_tool is not None
        factory_row = db.get(Factory, factory_id)
        assert factory_row is not None
        asyncio.run(execute_tool(db, factory_row, agent, task, "workspace", {"operation": "write", "path": "evidence.md", "content": "verified artifact"}))
        assert db.scalar(select(Event).where(Event.factory_id == factory_id, Event.event_type == "tool_called"))
        assert db.scalar(select(Artifact).where(Artifact.factory_id == factory_id, Artifact.name == "evidence.md")) is not None

        space = db.scalar(select(Space).where(Space.factory_id == factory_id))
        assert space is not None
        assert client.post(f"/api/factories/{factory_id}/messages", headers=auth, json={"message_type": "MESSAGE", "body": "review this", "sender_agent_id": agent.id, "recipient_agent_id": agent.id}).status_code == 201
        db.add_all([Task(factory_id=factory_id, title=f"queued-{i}") for i in range(4)])
        db.commit()
        asyncio.run(runtime._maybe_reorganize(factory_id))
        assert db.scalar(select(Event).where(Event.factory_id == factory_id, Event.event_type == "organization_changed"))

        stale = Task(factory_id=factory_id, title="stale", status="running", lease_until=utc_now() - timedelta(seconds=10), assignee_id=agent.id)
        db.add(stale)
        db.flush()
        agent.status = "working"
        agent.current_task_id = stale.id
        db.commit()
        asyncio.run(runtime.recover_abandoned_tasks())
        db.expire_all()
        recovered_task = db.get(Task, stale.id)
        recovered_agent = db.get(Agent, agent.id)
        assert recovered_task is not None and recovered_task.status == "queued"
        assert recovered_agent is not None and recovered_agent.status == "idle"
    finally:
        db.close()


def test_evaluator_does_not_complete_zero_task_or_unmet_goal(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="F", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        goal = Goal(factory_id=factory.id, title="Goal", objective="o", criteria=["required evidence"])
        db.add(goal)
        run = FactoryRun(factory_id=factory.id, status="running")
        db.add(run)
        db.commit()
        asyncio.run(Runtime()._evaluate(db, factory, run))
        db.refresh(goal)
        assert goal.status != "completed"
        assert goal.evaluation["passed"] is False
    finally:
        db.close()
