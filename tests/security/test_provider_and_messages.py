from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Event, Factory, Message
from app.provider import FakeProvider, ProviderResponse
from app.services import Runtime


def test_fake_provider_exposes_usage():
    response = asyncio.run(FakeProvider("ok", usage=ProviderResponse("ok", 2, 3, 5)).chat_with_usage([]))
    assert response.total_tokens == 5


def test_message_idempotency_and_delivery(client, auth):
    factory = client.post("/api/factories", headers=auth, json={"name": "Messages", "mission": "m", "primary_objective": "o", "provider_api_key": "secret"}).json()
    factory_id = factory["id"]
    payload = {"message_type": "TASK_REQUEST", "body": "do it", "idempotency_key": "same-key", "correlation_id": "corr-1"}
    first = client.post(f"/api/factories/{factory_id}/messages", headers=auth, json=payload)
    second = client.post(f"/api/factories/{factory_id}/messages", headers=auth, json=payload)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "queued"
    read = client.post(f"/api/factories/{factory_id}/messages/{first.json()['id']}/read", headers=auth)
    assert read.status_code == 200 and read.json()["status"] == "read"
