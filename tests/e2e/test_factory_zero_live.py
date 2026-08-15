from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
LIVE_MARKER = "Factory Zero completed explicit diagnose and plan phases during its second verified self-improvement."


@pytest.mark.factory_zero_live
def test_factory_zero_completes_one_real_self_change(client, auth):
    provider_key = os.getenv("OPENAI_API_KEY")
    github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not provider_key or not github_token:
        pytest.skip("set OPENAI_API_KEY and GITHUB_TOKEN for the real Factory Zero cycle")
    if (ROOT / "README.md").is_file() and LIVE_MARKER in (ROOT / "README.md").read_text(encoding="utf-8"):
        pytest.skip("the real Factory Zero marker is already merged; use a new objective for another cycle")

    created = client.post(
        "/api/factories",
        headers=auth,
        json={
            "name": "Factory Zero Live",
            "mission": "Continuously improve AI Factory OS itself",
            "primary_objective": "Produce one verified self-improvement",
            "autonomy": "fully_autonomous",
            "constraints": ["Preserve existing behavior and verify every change."],
            "provider_base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "provider_model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "provider_api_key": provider_key,
            "tool_permissions": ["workspace", "repository"],
        },
    )
    assert created.status_code == 201, created.text
    factory_id = created.json()["id"]

    architect = client.post(f"/api/factories/{factory_id}/architect", headers=auth)
    assert architect.status_code == 200, architect.text

    repository = client.post(
        f"/api/factories/{factory_id}/repositories",
        headers=auth,
        json={
            "owner": "TitoTFP",
            "name": "ai-factory-os",
            "default_branch": "master",
            "github_token": github_token,
            "test_commands": [[sys.executable, "-m", "pytest", "-q", "-m", "not factory_zero_live"]],
        },
    )
    assert repository.status_code == 200, repository.text

    cycle = client.post(
        f"/api/factories/{factory_id}/improvement-cycles",
        headers=auth,
        json={
            "repository_id": repository.json()["id"],
            "objective": "Add the exact sentence `Factory Zero completed explicit diagnose and plan phases during its second verified self-improvement.` to README.md under the Factory Zero safety model. Do not change the meaning of any other documentation, and keep all existing tests passing.",
        },
    )
    assert cycle.status_code == 200, cycle.text
    cycle_id = cycle.json()["id"]

    poll_text = os.getenv("FACTORY_ZERO_LIVE_POLLS", "450")
    try:
        poll_count = max(1, int(poll_text))
    except ValueError:
        poll_count = 450
    final = None
    for _ in range(poll_count):
        response = client.get(f"/api/factories/{factory_id}/improvement-cycles", headers=auth)
        assert response.status_code == 200, response.text
        final = next(item for item in response.json() if item["id"] == cycle_id)
        if final["status"] in {"completed", "failed"}:
            break
        time.sleep(2)

    assert final is not None
    assert final["status"] == "completed", (
        f"cycle status={final['status']} phase={final['phase']} "
        f"error={final['error']} retry_count={final['retry_count']}"
    )
    assert final["pr_number"]
    assert final["pr_url"].startswith("https://github.com/TitoTFP/ai-factory-os/pull/")
    assert final["review"]["approved"]
    assert final["verification"]["passed"]
    assert final["observation"]["merged"]

    snapshot = client.get(f"/api/factories/{factory_id}", headers=auth)
    assert snapshot.status_code == 200
    events = snapshot.json()["events"]
    event_types = {event["event_type"] for event in events}
    expected_events = {
        "improvement_cycle_discovered",
        "improvement_cycle_diagnosed",
        "improvement_cycle_planned",
        "improvement_cycle_verified",
        "improvement_cycle_reviewed",
        "improvement_cycle_merged",
        "improvement_cycle_completed",
        "repository_operation_started",
        "repository_operation_succeeded",
        "repository_command_started",
        "repository_command_succeeded",
        "github_operation_started",
        "github_operation_succeeded",
    }
    assert not expected_events - event_types, f"missing events={sorted(expected_events - event_types)} observed={sorted(event_types)}"
    merged_event = next(event for event in events if event["event_type"] == "improvement_cycle_merged" and event["payload"].get("cycle_id") == cycle_id)
    assert merged_event["payload"]["branch_name"]
    assert merged_event["payload"]["pr_number"] == final["pr_number"]
    assert merged_event["payload"]["reviewer_agent_id"] == final["reviewer_agent_id"]
    completed_event = next(event for event in events if event["event_type"] == "improvement_cycle_completed" and event["payload"].get("cycle_id") == cycle_id)
    assert completed_event["payload"]["merge_commit_sha"] == final["observation"]["merge_commit_sha"]

    audit_path = os.getenv("FACTORY_ZERO_LIVE_AUDIT_PATH")
    if audit_path:
        Path(audit_path).write_text(
            json.dumps(
                {
                    "cycle": final,
                    "events": [
                        {
                            "id": event.get("id"),
                            "event_type": event["event_type"],
                            "payload": event["payload"],
                        }
                        for event in events
                        if event["payload"].get("cycle_id") == cycle_id
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
