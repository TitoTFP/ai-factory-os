from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.config import settings
from app.models import Agent, Artifact, Event, Factory, FactoryCredential, FactoryRun, Goal, Message, Space, Task, Tool, Usage


def test_register_login_and_factory_isolation(client):
    first = client.post("/api/auth/register", json={"email": "one@example.com", "name": "One", "password": "password123"}).json()
    second = client.post("/api/auth/register", json={"email": "two@example.com", "name": "Two", "password": "password123"}).json()
    first_headers = {"Authorization": f"Bearer {first['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second['access_token']}"}
    created = client.post("/api/factories", headers=first_headers, json={"name": "One Factory", "mission": "Test", "primary_objective": "Ship", "provider_api_key": "secret", "provider_base_url": "https://api.openai.com/v1"})
    assert created.status_code == 201
    factory_id = created.json()["id"]
    assert client.get(f"/api/factories/{factory_id}", headers=first_headers).status_code == 200
    assert client.get(f"/api/factories/{factory_id}", headers=second_headers).status_code == 404
    assert client.get(f"/api/factories/{factory_id}/messages", headers=second_headers).status_code == 404
    assert client.get(f"/api/factories/{factory_id}/usage", headers=second_headers).status_code == 404
    assert client.post(f"/api/factories/{factory_id}/organization", headers=second_headers, json={"action": "hire"}).status_code == 404
    assert client.put(f"/api/factories/{factory_id}/credentials", headers=second_headers, json={"base_url": "https://api.openai.com/v1", "model": "x", "api_key": "secret", "permissions": []}).status_code == 404


def test_resource_rows_are_factory_scoped(database):
    db = SessionLocal()
    try:
        first = Factory(owner_id="u1", name="F1", mission="m", primary_objective="o", constraints=[])
        second = Factory(owner_id="u2", name="F2", mission="m", primary_objective="o", constraints=[])
        db.add_all([first, second])
        db.flush()
        db.add_all([Artifact(factory_id=first.id, name="a", content="x"), Event(factory_id=first.id, event_type="x"), FactoryCredential(factory_id=first.id, provider="openai-compatible", base_url="https://api.openai.com/v1", model="m", encrypted_api_key="encrypted"), Message(factory_id=first.id, message_type="MESSAGE", body="x"), Task(factory_id=first.id, title="x")])
        db.commit()
        assert all(row.factory_id == first.id for row in db.scalars(select(Artifact).where(Artifact.factory_id == first.id)))
        assert db.scalar(select(Artifact).where(Artifact.factory_id == second.id)) is None
        assert db.scalar(select(Event).where(Event.factory_id == second.id)) is None
        assert db.scalar(select(Message).where(Message.factory_id == second.id)) is None
        assert db.scalar(select(Task).where(Task.factory_id == second.id)) is None
    finally:
        db.close()


def test_foreign_tenant_cannot_mutate_any_factory_resource(client, database):
    first = client.post("/api/auth/register", json={"email": "mutation-one@example.com", "name": "One", "password": "password123"}).json()
    second = client.post("/api/auth/register", json={"email": "mutation-two@example.com", "name": "Two", "password": "password123"}).json()
    first_headers = {"Authorization": f"Bearer {first['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second['access_token']}"}
    first_factory = client.post(
        "/api/factories",
        headers=first_headers,
        json={"name": "First", "mission": "m", "primary_objective": "o", "provider_api_key": "secret"},
    ).json()["id"]
    second_factory = client.post(
        "/api/factories",
        headers=second_headers,
        json={"name": "Second", "mission": "m", "primary_objective": "o", "provider_api_key": "secret"},
    ).json()["id"]

    db = SessionLocal()
    try:
        first_space = Space(factory_id=first_factory, name="First space", purpose="p")
        second_space = Space(factory_id=second_factory, name="Second space", purpose="p")
        db.add_all([first_space, second_space])
        db.flush()
        first_agent = Agent(factory_id=first_factory, space_id=first_space.id, name="First agent", role="r")
        second_agent = Agent(factory_id=second_factory, space_id=second_space.id, name="Second agent", role="r")
        first_goal = Goal(factory_id=first_factory, title="First goal", objective="o", criteria=["e"])
        second_goal = Goal(factory_id=second_factory, title="Second goal", objective="o", criteria=["e"])
        first_task = Task(factory_id=first_factory, title="First task")
        second_task = Task(factory_id=second_factory, title="Second task")
        first_message = Message(factory_id=first_factory, message_type="MESSAGE", body="first")
        db.add_all([first_agent, second_agent, first_goal, second_goal, first_task, second_task, first_message])
        db.add_all([
            FactoryRun(factory_id=first_factory, status="stopped"),
            FactoryRun(factory_id=second_factory, status="stopped"),
            Tool(factory_id=first_factory, name="workspace", permissions=["read"]),
            Tool(factory_id=second_factory, name="workspace", permissions=["read"]),
            Usage(factory_id=first_factory),
            Usage(factory_id=second_factory),
            Artifact(factory_id=first_factory, name="first", content="x"),
            Artifact(factory_id=second_factory, name="second", content="x"),
            Event(factory_id=first_factory, event_type="first"),
            Event(factory_id=second_factory, event_type="second"),
        ])
        db.commit()
        ids = {"space": first_space.id, "agent": first_agent.id, "goal": first_goal.id, "task": first_task.id, "message": first_message.id}
        foreign_ids = {"space": second_space.id, "agent": second_agent.id, "goal": second_goal.id, "task": second_task.id}
    finally:
        db.close()

    # Every mutating factory route must reject the owner and resources of another tenant.
    attempts = [
        ("post", f"/api/factories/{first_factory}/run", {}),
        ("post", f"/api/factories/{first_factory}/resume", {}),
        ("post", f"/api/factories/{first_factory}/pause", {}),
        ("post", f"/api/factories/{first_factory}/stop", {}),
        ("put", f"/api/factories/{first_factory}/credentials", {"base_url": "https://api.openai.com/v1", "model": "x", "api_key": "secret", "permissions": []}),
        ("post", f"/api/factories/{first_factory}/organization", {"action": "hibernate", "agent_id": ids["agent"]}),
        ("post", f"/api/factories/{first_factory}/tasks", {"title": "attack", "assignee_id": ids["agent"], "goal_id": ids["goal"], "parent_id": ids["task"]}),
        ("post", f"/api/factories/{first_factory}/messages", {"message_type": "MESSAGE", "body": "attack", "sender_agent_id": ids["agent"], "recipient_agent_id": ids["agent"]}),
        ("post", f"/api/factories/{first_factory}/messages/{ids['message']}/read", {}),
        ("post", f"/api/factories/{first_factory}/goals/{ids['goal']}/complete", {}),
    ]
    for method, path, payload in attempts:
        response = getattr(client, method)(path, headers=second_headers, json=payload or None)
        assert response.status_code == 404, f"{method.upper()} {path}: {response.status_code}"

    # Even the legitimate owner cannot attach a resource belonging to another factory.
    foreign_attempts = [
        ("post", f"/api/factories/{first_factory}/organization", {"action": "hibernate", "agent_id": foreign_ids["agent"]}, 422),
        ("post", f"/api/factories/{first_factory}/organization", {"action": "move", "agent_id": ids["agent"], "space_id": foreign_ids["space"]}, 422),
        ("post", f"/api/factories/{first_factory}/tasks", {"title": "cross-agent", "assignee_id": foreign_ids["agent"]}, 404),
        ("post", f"/api/factories/{first_factory}/tasks", {"title": "cross-goal", "goal_id": foreign_ids["goal"]}, 404),
        ("post", f"/api/factories/{first_factory}/tasks", {"title": "cross-parent", "parent_id": foreign_ids["task"]}, 404),
        ("post", f"/api/factories/{first_factory}/messages", {"message_type": "MESSAGE", "body": "cross-agent", "sender_agent_id": foreign_ids["agent"]}, 404),
    ]
    for method, path, payload, expected in foreign_attempts:
        response = getattr(client, method)(path, headers=first_headers, json=payload)
        assert response.status_code == expected, f"{method.upper()} {path}: {response.status_code}"

    assert client.get(f"/api/factories/{first_factory}", headers=second_headers).status_code == 404
    assert client.get(f"/api/factories/{first_factory}/messages", headers=second_headers).status_code == 404
    assert client.get(f"/api/factories/{first_factory}/usage", headers=second_headers).status_code == 404
    assert client.post(f"/api/factories/{first_factory}/architect", headers=second_headers).status_code == 404
    assert client.post(f"/api/factories/{first_factory}/tasks", headers=second_headers, json={"title": "foreign"}).status_code == 404
    assert client.post(f"/api/factories/{first_factory}/messages", headers=second_headers, json={"message_type": "MESSAGE", "body": "foreign"}).status_code == 404
    assert client.post(f"/api/factories/{first_factory}/goals/{ids['goal']}/complete", headers=second_headers).status_code == 404

    db = SessionLocal()
    try:
        first_agent_row = db.get(Agent, ids["agent"])
        first_goal_row = db.get(Goal, ids["goal"])
        first_task_row = db.get(Task, ids["task"])
        first_message_row = db.get(Message, ids["message"])
        second_agent_row = db.get(Agent, foreign_ids["agent"])
        second_goal_row = db.get(Goal, foreign_ids["goal"])
        second_task_row = db.get(Task, foreign_ids["task"])
        first_artifact_row = db.scalar(select(Artifact).where(Artifact.factory_id == first_factory))
        second_artifact_row = db.scalar(select(Artifact).where(Artifact.factory_id == second_factory))
        assert first_agent_row is not None and first_agent_row.status == "idle"
        assert first_goal_row is not None and first_goal_row.status == "pending"
        assert first_task_row is not None and first_task_row.status == "queued"
        assert first_message_row is not None and first_message_row.status == "queued"
        assert second_agent_row is not None and second_agent_row.status == "idle"
        assert second_goal_row is not None and second_goal_row.status == "pending"
        assert second_task_row is not None and second_task_row.status == "queued"
        assert first_artifact_row is not None and first_artifact_row.name == "first"
        assert second_artifact_row is not None and second_artifact_row.name == "second"
        first_event_types = {event.event_type for event in db.scalars(select(Event).where(Event.factory_id == first_factory))}
        assert "goal_completed_by_user" not in first_event_types
        assert "message_published" not in first_event_types
    finally:
        db.close()


def test_password_and_oauth_login(client, monkeypatch):
    client.post("/api/auth/register", json={"email": "auth@example.com", "name": "Auth", "password": "password123"})
    assert client.post("/api/auth/login", json={"email": "auth@example.com", "password": "wrongpass"}).status_code == 401

    assert client.post("/api/auth/oauth/callback", json={"provider": "github", "code": "untrusted", "state": "untrusted"}).status_code == 400

    async def verified(provider, code, redirect_uri, settings):
        return {"subject": f"{provider}-1", "email": f"{provider}@example.com", "name": provider.title()}

    monkeypatch.setattr("app.main.verify_oauth_code", verified)
    for provider in ("github", "google"):
        started = client.post("/api/auth/oauth/start", json={"provider": provider})
        assert started.status_code == 503

    object.__setattr__(settings, "oauth_github_client_id", "github-client")
    object.__setattr__(settings, "oauth_google_client_id", "google-client")
    for provider in ("github", "google"):
        started = client.post("/api/auth/oauth/start", json={"provider": provider})
        assert started.status_code == 200
        response = client.post("/api/auth/oauth/callback", json={"provider": provider, "code": "verified-code", "state": started.json()["state"]})
        assert response.status_code == 200
        assert response.json()["user"]["email"] == f"{provider}@example.com"
