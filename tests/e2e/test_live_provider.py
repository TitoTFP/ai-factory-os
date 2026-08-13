from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Artifact, Event, Task, Usage


pytestmark = pytest.mark.live


def test_configured_provider_drives_architect_and_agent(client, auth):
    missing = [
        name
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
        if not os.getenv(name)
    ]
    if missing:
        pytest.skip(f"live provider configuration missing: {', '.join(missing)}")

    factory_response = client.post(
        "/api/factories",
        headers=auth,
        json={
            "name": f"Live E2E {uuid4().hex[:8]}",
            "mission": "Verify a configured OpenAI-compatible provider",
            "primary_objective": "Produce live provider evidence",
            "provider_api_key": os.environ["OPENAI_API_KEY"],
            "provider_base_url": os.environ["OPENAI_BASE_URL"],
            "provider_model": os.environ["OPENAI_MODEL"],
            "tool_permissions": ["workspace"],
        },
    )
    assert factory_response.status_code == 201, factory_response.text
    factory_id = factory_response.json()["id"]

    architect_response = client.post(f"/api/factories/{factory_id}/architect", headers=auth)
    assert architect_response.status_code == 200, architect_response.text
    plan = architect_response.json()
    assert len(plan["spaces"]) >= 2
    assert len(plan["agents"]) >= 3
    assert plan["goals"]

    run_response = client.post(f"/api/factories/{factory_id}/run", headers=auth)
    assert run_response.status_code == 200, run_response.text

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            task = db.scalar(select(Task).where(Task.factory_id == factory_id).order_by(Task.created_at))
            if task and task.status in {"done", "failed"}:
                assert task.status == "done", task.error
                artifact = db.scalar(select(Artifact).where(Artifact.factory_id == factory_id, Artifact.task_id == task.id))
                usage = db.scalar(select(Usage).where(Usage.factory_id == factory_id, Usage.task_id == task.id))
                completed = db.scalar(select(Event).where(Event.factory_id == factory_id, Event.event_type == "task_completed"))
                assert artifact is not None
                assert usage is not None and usage.total_tokens > 0
                assert completed is not None
                return
        finally:
            db.close()
        time.sleep(0.25)

    pytest.fail("configured provider agent task did not finish within 120 seconds")
