from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, Event, Factory, FactoryRun, Space, Task, Tool, now
from app.services import Runtime, execute_tool, validate_external_url
from app.security import utc_now


def test_url_policy_blocks_private_networks():
    for url in ("http://localhost/", "http://127.0.0.1/", "http://169.254.169.254/latest", "http://10.0.0.1/"):
        with pytest.raises(ValueError):
            validate_external_url(url)


def test_runtime_records_provider_failures(database, monkeypatch):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="F", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        run = FactoryRun(factory_id=factory.id, status="running")
        db.add(run)
        db.commit()
        async def fail(_self, _factory_id):
            raise RuntimeError("provider exploded")
        monkeypatch.setattr(Runtime, "process_factory", fail)
        runtime = Runtime()
        asyncio.run(runtime._record_runtime_error(factory.id, RuntimeError("provider exploded")))
        db.expire_all()
        persisted = db.get(FactoryRun, run.id)
        assert persisted is not None and persisted.last_error == "provider exploded"
        assert db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "runtime_error")) is not None
    finally:
        db.close()


def test_recovery_requeues_expired_tasks(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="F", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        task = Task(factory_id=factory.id, title="stuck", status="running", lease_until=utc_now() - timedelta(seconds=10))
        db.add(task)
        db.commit()
        asyncio.run(Runtime().recover_abandoned_tasks())
        db.refresh(task)
        assert task.status == "queued"
        assert db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "task_recovered")) is not None
    finally:
        db.close()


def test_event_stream_continues_past_first_hundred_events(client, auth, database):
    created = client.post(
        "/api/factories",
        headers=auth,
        json={"name": "Live", "mission": "m", "primary_objective": "o", "provider_api_key": "secret"},
    )
    factory_id = created.json()["id"]
    db = SessionLocal()
    try:
        base = now()
        db.add_all([
            Event(factory_id=factory_id, event_type=f"event-{index}", payload={"n": index}, created_at=base + timedelta(seconds=index))
            for index in range(105)
        ])
        db.commit()
    finally:
        db.close()
    token = auth["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(f"/api/factories/{factory_id}/events?token={token}") as websocket:
        first = websocket.receive_json()
        assert first["payload"]["n"] == 5


def test_tool_event_redacts_nested_secrets(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="F", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        space = Space(factory_id=factory.id, name="S", purpose="p")
        db.add(space)
        db.flush()
        agent = Agent(factory_id=factory.id, space_id=space.id, name="A", role="R", objective="O")
        db.add(agent)
        db.add(Tool(factory_id=factory.id, name="workspace", enabled=True, permissions=["read", "write"]))
        db.commit()
        asyncio.run(execute_tool(db, factory, agent, None, "workspace", {"operation": "write", "path": "secret.md", "content": "safe", "headers": {"Authorization": "secret"}}))
        event = db.scalar(select(Event).where(Event.factory_id == factory.id, Event.event_type == "tool_called"))
        assert event is not None
        assert "Authorization" not in str(event.payload)
        assert "[REDACTED]" in str(event.payload)
    finally:
        db.close()
