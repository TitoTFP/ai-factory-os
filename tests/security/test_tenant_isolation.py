from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.config import settings
from app.models import Artifact, Event, Factory, FactoryCredential, Message, Task


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
