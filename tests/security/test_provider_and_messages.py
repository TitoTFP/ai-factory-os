from __future__ import annotations

import asyncio
import json

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, Event, Factory, FactoryRun, Message, Space, Task, Tool
from app.provider import FakeProvider, OpenAICompatibleProvider, ProviderConfig, ProviderResponse
from app.services import Runtime


def test_fake_provider_exposes_usage():
    response = asyncio.run(FakeProvider("ok", usage=ProviderResponse("ok", 2, 3, 5)).chat_with_usage([]))
    assert response.total_tokens == 5


def test_openai_compatible_adapter_parses_tool_calls_and_usage():
    async def scenario():
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["tools"][0]["function"]["name"] == "workspace"
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "workspace", "arguments": '{"operation":"write"}'}}]}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OpenAICompatibleProvider(ProviderConfig("https://provider.test/v1", "test-model", "secret"), client=client)
            response = await provider.chat_with_usage(
                [{"role": "user", "content": "work"}],
                tools=[{"type": "function", "function": {"name": "workspace"}}],
            )
            assert response.total_tokens == 11
            assert response.tool_calls[0].name == "workspace"
            assert response.tool_calls[0].arguments == {"operation": "write"}
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_model_tool_schema_exposes_move_responsibility_and_provider_call(database):
    db = SessionLocal()
    try:
        factory = Factory(owner_id="owner", name="Org", mission="m", primary_objective="o", constraints=[])
        db.add(factory)
        db.flush()
        space = Space(factory_id=factory.id, name="Work", purpose="p")
        db.add(space)
        db.flush()
        agent = Agent(factory_id=factory.id, space_id=space.id, name="A", role="r", objective="o", responsibilities=["research"])
        target = Agent(factory_id=factory.id, space_id=space.id, name="B", role="r", objective="o")
        task = Task(factory_id=factory.id, assignee_id=agent.id, title="organize")
        db.add_all([agent, target, task, FactoryRun(factory_id=factory.id, status="running"), Tool(factory_id=factory.id, name="workspace", enabled=True, permissions=["read"])])
        db.commit()
        schema = Runtime._agent_tools(db, factory.id)
        reorganize = next(item for item in schema if item["function"]["name"] == "reorganize")
        assert "move_responsibility" in reorganize["function"]["parameters"]["properties"]["action"]["enum"]
        result = asyncio.run(Runtime()._model_action(db, factory, agent, task, "reorganize", {"action": "move_responsibility", "agent_id": agent.id, "target_agent_id": target.id}))
        assert result["action"] == "move_responsibility"
    finally:
        db.close()


def test_message_idempotency_and_delivery(client, auth):
    factory = client.post("/api/factories", headers=auth, json={"name": "Messages", "mission": "m", "primary_objective": "o", "provider_api_key": "secret", "tool_permissions": ["workspace", "http"]}).json()
    factory_id = factory["id"]
    db = SessionLocal()
    try:
        event = db.scalar(select(Event).where(Event.factory_id == factory_id, Event.event_type == "factory_created"))
        assert event is not None
        assert event.payload["permissions"] == ["http", "workspace"]
        assert event.payload["model"]
    finally:
        db.close()
    payload = {"message_type": "TASK_REQUEST", "body": "do it", "idempotency_key": "same-key", "correlation_id": "corr-1"}
    first = client.post(f"/api/factories/{factory_id}/messages", headers=auth, json=payload)
    second = client.post(f"/api/factories/{factory_id}/messages", headers=auth, json=payload)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "queued"
    read = client.post(f"/api/factories/{factory_id}/messages/{first.json()['id']}/read", headers=auth)
    assert read.status_code == 200 and read.json()["status"] == "read"
