from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, Artifact, Event, Factory, FactoryRun, Goal, Message, Space, Task, Tool
from app.services import Runtime, execute_tool
from app.provider import ProviderResponse, ToolCall
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


def test_runtime_emits_result_message_for_autonomous_tool_task(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="Messaging", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        space = Space(factory_id=factory.id, name="Work", purpose="Execute work")
        db.add(space)
        db.flush()
        agent = Agent(factory_id=factory.id, space_id=space.id, name="Worker", role="Worker", objective="o")
        db.add(agent)
        db.flush()
        task = Task(
            factory_id=factory.id,
            assignee_id=agent.id,
            title="Write report",
            inputs={"tool": "workspace", "operation": "write", "path": "report.md", "content": "done"},
        )
        db.add_all([task, Tool(factory_id=factory.id, name="workspace", enabled=True, permissions=["read", "write"]), FactoryRun(factory_id=factory.id, status="running")])
        db.commit()

        asyncio.run(Runtime().process_factory(factory.id))
        db.expire_all()
        completed = db.get(Task, task.id)
        result = db.scalar(select(Message).where(Message.factory_id == factory.id, Message.message_type == "TASK_RESULT"))
        tool_event = db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "tool_called"))
        artifact = db.scalar(select(Artifact).where(Artifact.factory_id == factory.id, Artifact.name == "report.md"))
        assert completed is not None and completed.status == "done"
        assert result is not None and result.payload["task_id"] == task.id
        assert result.sender_agent_id == agent.id
        assert tool_event is not None and tool_event.payload["tool"] == "workspace"
        assert artifact is not None and artifact.content == "done"
    finally:
        db.close()


def test_agent_delegation_and_review_round_trip(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="Delegation", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        space = Space(factory_id=factory.id, name="Work", purpose="Execute work")
        db.add(space)
        db.flush()
        sender = Agent(factory_id=factory.id, space_id=space.id, name="Sender", role="Builder", objective="o")
        recipient = Agent(factory_id=factory.id, space_id=space.id, name="Recipient", role="Reviewer", objective="o")
        db.add_all([sender, recipient])
        db.flush()
        artifact = Artifact(factory_id=factory.id, agent_id=sender.id, name="draft.md", content="draft")
        task = Task(factory_id=factory.id, assignee_id=sender.id, title="Delegate", description="delegate", inputs={})
        db.add_all([artifact, task, FactoryRun(factory_id=factory.id, status="running")])
        db.commit()
        runtime = Runtime()
        asyncio.run(runtime._model_action(db, factory, sender, task, "delegate_task", {"agent_id": recipient.id, "title": "Review draft", "description": "Review it"}))
        asyncio.run(runtime._process_inbox(db, factory))
        delegated = db.scalar(select(Task).where(Task.assignee_id == recipient.id, Task.title == "Review draft"))
        assert delegated is not None
        assert delegated.inputs["reply_to_agent_id"] == sender.id
        asyncio.run(runtime._model_action(db, factory, sender, task, "request_review", {"agent_id": recipient.id, "artifact_id": artifact.id, "instructions": "Check draft"}))
        asyncio.run(runtime._process_inbox(db, factory))
        review = db.scalar(select(Task).where(Task.assignee_id == recipient.id, Task.inputs["review_artifact_id"].as_string() == artifact.id))
        assert review is not None
    finally:
        db.close()


def test_provider_issued_reorganization_call_is_applied(database, monkeypatch):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="Provider Org", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        space = Space(factory_id=factory.id, name="Work", purpose="p")
        db.add(space)
        db.flush()
        source = Agent(factory_id=factory.id, space_id=space.id, name="Source", role="r", objective="o", responsibilities=["research"])
        target = Agent(factory_id=factory.id, space_id=space.id, name="Target", role="r", objective="o")
        task = Task(factory_id=factory.id, assignee_id=source.id, title="Reorganize", description="Move responsibility")
        db.add_all([source, target, task, FactoryRun(factory_id=factory.id, status="running")])
        db.commit()

        class ToolCallingProvider:
            config = type("Config", (), {"model": "test-model"})()
            last_response = ProviderResponse("", total_tokens=1, tool_calls=(ToolCall("call-1", "reorganize", {"action": "move_responsibility", "agent_id": source.id, "target_agent_id": target.id}),))

            async def chat(self, messages, *, json_mode=False, tools=None):
                return "reorganized"

        monkeypatch.setattr("app.services.provider_for", lambda _db, _factory_id: ToolCallingProvider())
        asyncio.run(Runtime().process_factory(factory.id))
        db.expire_all()
        persisted_target = db.get(Agent, target.id)
        persisted_task = db.get(Task, task.id)
        assert persisted_target is not None and "research" in persisted_target.responsibilities
        assert persisted_task is not None and persisted_task.status == "done"
        assert db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "organization_changed")) is not None
    finally:
        db.close()


def test_terminal_failure_escalates_and_retries(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="Escalation", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        task = Task(factory_id=factory.id, title="Will fail", inputs={"tool": "missing"}, max_retries=0)
        db.add_all([task, FactoryRun(factory_id=factory.id, status="running")])
        db.commit()
        asyncio.run(Runtime().process_factory(factory.id))
        db.expire_all()
        failed_task = db.get(Task, task.id)
        assert failed_task is not None and failed_task.status == "failed"
        assert db.scalar(select(Message).where(Message.factory_id == factory.id, Message.message_type == "ESCALATION")) is not None
        assert db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "task_escalated")) is not None
    finally:
        db.close()


def test_runtime_restart_requeues_and_resumes_stale_work(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="Restart", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        space = Space(factory_id=factory.id, name="Work", purpose="Execute work")
        db.add(space)
        db.flush()
        agent = Agent(factory_id=factory.id, space_id=space.id, name="Worker", role="Worker", objective="o", status="working")
        db.add(agent)
        db.flush()
        task = Task(
            factory_id=factory.id,
            assignee_id=agent.id,
            title="Recover report",
            status="running",
            lease_until=utc_now() - timedelta(seconds=10),
            inputs={"tool": "workspace", "operation": "write", "path": "recovered.md", "content": "recovered"},
        )
        db.add(task)
        db.flush()
        agent.current_task_id = task.id
        db.add_all([Tool(factory_id=factory.id, name="workspace", enabled=True, permissions=["read", "write"]), FactoryRun(factory_id=factory.id, status="running")])
        db.commit()

        runtime = Runtime()
        asyncio.run(runtime.recover_abandoned_tasks())
        db.expire_all()
        recovered = db.get(Task, task.id)
        recovered_agent = db.get(Agent, agent.id)
        assert recovered is not None and recovered.status == "queued"
        assert recovered_agent is not None and recovered_agent.status == "idle"
        asyncio.run(runtime.process_factory(factory.id))
        db.expire_all()
        resumed = db.get(Task, task.id)
        assert resumed is not None and resumed.status == "done"
        assert db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "task_recovered")) is not None
        assert db.scalar(select(Message).where(Message.factory_id == factory.id, Message.message_type == "TASK_RESULT")) is not None
    finally:
        db.close()


def test_reorganization_actions_are_durable(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="Org", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        first_space = Space(factory_id=factory.id, name="First", purpose="p")
        second_space = Space(factory_id=factory.id, name="Second", purpose="p")
        db.add_all([first_space, second_space])
        db.flush()
        source = Agent(factory_id=factory.id, space_id=first_space.id, name="Source", role="r", responsibilities=["research", "write"])
        target = Agent(factory_id=factory.id, space_id=second_space.id, name="Target", role="r", responsibilities=["review"])
        task = Task(factory_id=factory.id, assignee_id=source.id, title="queued")
        db.add_all([source, target])
        db.flush()
        task = Task(factory_id=factory.id, assignee_id=source.id, title="queued")
        db.add(task)
        db.commit()
        runtime = Runtime()
        asyncio.run(runtime.reorganize(factory.id, "move_responsibility", agent_id=source.id, target_agent_id=target.id))
        asyncio.run(runtime.reorganize(factory.id, "move", agent_id=source.id, space_id=second_space.id))
        asyncio.run(runtime.reorganize(factory.id, "merge", agent_id=source.id, target_agent_id=target.id))
        db.expire_all()
        merged_source = db.get(Agent, source.id)
        merged_target = db.get(Agent, target.id)
        reassigned_task = db.get(Task, task.id)
        assert merged_source is not None and merged_source.status == "merged"
        assert merged_target is not None and "research" in merged_target.responsibilities and "write" in merged_target.responsibilities
        assert reassigned_task is not None and reassigned_task.assignee_id == target.id
        assert db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "organization_changed")) is not None
    finally:
        db.close()


def test_queue_pressure_reorganization_does_not_cross_factories(database):
    db = SessionLocal()
    try:
        pressured = Factory(owner_id="owner", name="Pressured", mission="m", primary_objective="o", constraints=[])
        quiet = Factory(owner_id="owner", name="Quiet", mission="m", primary_objective="o", constraints=[])
        db.add_all([pressured, quiet])
        db.flush()
        pressured_space = Space(factory_id=pressured.id, name="Work", purpose="p")
        quiet_space = Space(factory_id=quiet.id, name="Work", purpose="p")
        db.add_all([pressured_space, quiet_space])
        db.flush()
        db.add_all([Task(factory_id=pressured.id, title=f"queued-{i}") for i in range(4)])
        db.commit()

        asyncio.run(Runtime()._maybe_reorganize(pressured.id))
        db.expire_all()
        assert db.scalar(select(Agent).where(Agent.factory_id == pressured.id, Agent.role == "Load Balancer")) is not None
        assert db.scalar(select(Agent).where(Agent.factory_id == quiet.id, Agent.role == "Load Balancer")) is None
        assert db.scalar(select(Event).where(Event.factory_id == pressured.id, Event.event_type == "organization_changed")) is not None
        assert db.scalar(select(Event).where(Event.factory_id == quiet.id, Event.event_type == "organization_changed")) is None
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
