from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, Artifact, Event, Factory, FactoryRun, Space, Tool
from app.services import Runtime, execute_tool, safe_workspace_path


def test_architect_persists_plan_and_runtime_creates_artifact(client, auth, monkeypatch):
    created = client.post(
        "/api/factories",
        headers=auth,
        json={
            "name": "Runtime Factory",
            "mission": "Research useful signals",
            "primary_objective": "Create a report",
            "provider_api_key": "secret",
            "tool_permissions": ["workspace", "web_fetch", "http"],
        },
    )
    factory_id = created.json()["id"]
    plan = json.dumps({
        "spaces": [{"name": "Research", "purpose": "Find signals"}, {"name": "Review", "purpose": "Review findings"}],
        "agents": [
            {"name": "Scout", "role": "Researcher", "space": "Research", "objective": "Find signals", "responsibilities": ["search"]},
            {"name": "Analyst", "role": "Analyst", "space": "Research", "objective": "Analyze signals", "responsibilities": ["analyze"]},
            {"name": "Reviewer", "role": "Reviewer", "space": "Review", "objective": "Review report", "responsibilities": ["review"]},
        ],
        "goals": [{"title": "First report", "objective": "Create a report", "criteria": ["artifact exists"]}],
    })

    async def fake_chat(self, messages, *, json_mode=False):
        return plan

    monkeypatch.setattr("app.services.OpenAICompatibleProvider.chat", fake_chat)
    response = client.post(f"/api/factories/{factory_id}/architect", headers=auth)
    assert response.status_code == 200
    assert len(response.json()["spaces"]) == 2
    assert len(response.json()["agents"]) == 3

    db = SessionLocal()
    try:
        factory = db.get(Factory, factory_id)
        run = FactoryRun(factory_id=factory_id, status="running")
        db.add(run)
        db.commit()
        asyncio.run(Runtime().process_factory(factory_id))
        artifact = db.scalar(select(Artifact).where(Artifact.factory_id == factory_id))
        assert artifact is not None
        assert db.scalar(select(Event).where(Event.factory_id == factory_id, Event.event_type == "task_completed")) is not None
        asyncio.run(Runtime().process_factory(factory_id))
        assert db.scalar(select(Event).where(Event.factory_id == factory_id, Event.event_type == "goal_evaluated")) is not None
    finally:
        db.close()


def test_workspace_boundary_and_tool_audit(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="u", name="F", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        space = Space(factory_id=factory.id, name="S", purpose="p")
        db.add(space)
        db.flush()
        agent = Agent(factory_id=factory.id, space_id=space.id, name="A", role="R", objective="O")
        db.add(agent)
        db.add(Tool(factory_id=factory.id, name="workspace", enabled=True, permissions=["read", "write"]))
        db.commit()
        try:
            safe_workspace_path(factory.id, "../../outside")
            assert False, "path traversal should fail"
        except ValueError:
            pass
        asyncio.run(execute_tool(db, factory, agent, None, "workspace", {"operation": "write", "path": "notes.md", "content": "hello"}))
        assert db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "tool_called")) is not None
    finally:
        db.close()
