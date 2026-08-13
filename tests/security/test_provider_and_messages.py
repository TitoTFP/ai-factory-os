from __future__ import annotations

import asyncio
import json

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Event, Factory, Message
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
