from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from app.db import SessionLocal
from app.models import Agent, Factory, ImprovementCycle, Repository, Space, now
from app.provider import ProviderResponse, ToolCall
from app.services import Runtime


@pytest.mark.factory_zero
def test_factory_zero_cycle_is_durable_and_requires_independent_review(database, monkeypatch, tmp_path):
    db = SessionLocal()
    factory = Factory(owner_id="u", name="Factory Zero", mission="m", primary_objective="o", constraints=[])
    db.add(factory)
    db.flush()
    space = Space(factory_id=factory.id, name="Engineering", purpose="Build")
    db.add(space)
    db.flush()
    author = Agent(factory_id=factory.id, space_id=space.id, name="Builder", role="Implementer", objective="o", responsibilities=["build"])
    reviewer = Agent(factory_id=factory.id, space_id=space.id, name="Reviewer", role="Reviewer", objective="o", responsibilities=["review"])
    repository = Repository(
        factory_id=factory.id,
        owner="TitoTFP",
        name="ai-factory-os",
        remote_url="https://github.com/TitoTFP/ai-factory-os.git",
        default_branch="master",
        test_commands=[["python", "-m", "pytest", "-q"]],
    )
    db.add_all([author, reviewer, repository])
    db.flush()
    cycle = ImprovementCycle(
        factory_id=factory.id,
        repository_id=repository.id,
        objective="Make one verified improvement",
        author_agent_id=author.id,
        reviewer_agent_id=reviewer.id,
    )
    db.add(cycle)
    db.commit()

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "README.md").write_text("before", encoding="utf-8")

    class CycleProvider:
        config = type("Config", (), {"model": "test-model"})()

        def __init__(self):
            self.last_response = ProviderResponse("", total_tokens=1)
            self.implementation_tool_sent = False

        async def chat(self, messages, *, json_mode=False, tools=None):
            prompt = str(messages[-1].get("content", ""))
            if "Implement the objective" in prompt and not self.implementation_tool_sent:
                self.implementation_tool_sent = True
                self.last_response = ProviderResponse(
                    "",
                    total_tokens=1,
                    tool_calls=(ToolCall("write-1", "repository", {"operation": "write", "path": "README.md", "content": "after"}),),
                )
                return ""
            if "Implement the objective" in prompt:
                self.last_response = ProviderResponse(json.dumps({"summary": "updated README", "commit_message": "Factory Zero improvement"}), total_tokens=1)
                return self.last_response.content
            if "Review the exact" in prompt:
                self.last_response = ProviderResponse(json.dumps({"approved": True, "summary": "verified patch", "findings": [], "risks": []}), total_tokens=1)
                return self.last_response.content
            self.last_response = ProviderResponse(json.dumps({"problem": "outdated README", "proposed_change": "refresh README"}), total_tokens=1)
            return self.last_response.content

    provider = CycleProvider()
    monkeypatch.setattr("app.services.provider_for", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr("app.services.repository_token_for", lambda *_args, **_kwargs: "test-token")

    def fake_worktree(_repository, cycle_id, _token):
        return worktree, f"factory-zero/{cycle_id[:8]}", "b" * 40

    async def fake_repository_tool(_db, _factory, _agent, _task, arguments):
        if arguments.get("operation") == "write":
            (worktree / arguments["path"]).write_text(arguments["content"], encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr("app.services.create_worktree", fake_worktree)
    monkeypatch.setattr("app.services.execute_repository_tool", fake_repository_tool)
    monkeypatch.setattr("app.services.run_configured_commands", lambda *_args, **_kwargs: {"kind": "test", "passed": True, "commands": [{"returncode": 0}]})
    monkeypatch.setattr("app.services.git_status", lambda *_args, **_kwargs: {"status": "M README.md", "diff_stat": "README.md | 1"})
    monkeypatch.setattr("app.services.git_diff", lambda *_args, **_kwargs: {"diff": "-before\n+after"})
    monkeypatch.setattr("app.services.git_commit", lambda *_args, **_kwargs: "c" * 40)
    monkeypatch.setattr("app.services.push_branch", lambda *_args, **_kwargs: None)
    async def fake_create_pull_request(*_args, **_kwargs):
        return {"number": 7, "html_url": "https://github.com/TitoTFP/ai-factory-os/pull/7", "head": {"sha": "c" * 40}}

    async def fake_merge_pull_request(*_args, **_kwargs):
        return {"merged": True, "sha": "m" * 40}

    async def fake_pull_request(*_args, **_kwargs):
        return {"merged": True, "state": "closed", "merge_commit_sha": "m" * 40}

    async def fake_check_runs(*_args, **_kwargs):
        return {"total_count": 1, "check_runs": [{"conclusion": "success"}]}

    monkeypatch.setattr("app.services.create_pull_request", fake_create_pull_request)
    monkeypatch.setattr("app.services.merge_pull_request", fake_merge_pull_request)
    monkeypatch.setattr("app.services.pull_request", fake_pull_request)
    monkeypatch.setattr("app.services.check_runs", fake_check_runs)
    monkeypatch.setattr("app.services.cleanup_worktree", lambda *_args, **_kwargs: None)

    runtime = Runtime()
    expected_phases = ["implement", "verify", "review", "merge", "observe", "completed"]
    for expected in expected_phases:
        db.refresh(cycle)
        cycle.status = "running"
        db.commit()
        asyncio.run(runtime._advance_improvement_cycle(db, factory, cycle.id))
        db.refresh(cycle)
        assert cycle.phase == expected

    assert cycle.status == "completed"
    assert cycle.review["approved"]
    assert cycle.observation["merged"]
    assert (worktree / "README.md").read_text(encoding="utf-8") == "after"
    db.close()


@pytest.mark.factory_zero
def test_runtime_requeues_stale_improvement_cycle(database):
    db = SessionLocal()
    factory = Factory(owner_id="u", name="Recovery Factory", mission="m", primary_objective="o", constraints=[])
    db.add(factory)
    db.flush()
    space = Space(factory_id=factory.id, name="Engineering", purpose="Build")
    db.add(space)
    db.flush()
    author = Agent(factory_id=factory.id, space_id=space.id, name="Builder", role="Implementer", objective="o", responsibilities=["build"])
    reviewer = Agent(factory_id=factory.id, space_id=space.id, name="Reviewer", role="Reviewer", objective="o", responsibilities=["review"])
    repository = Repository(factory_id=factory.id, owner="TitoTFP", name="ai-factory-os", remote_url="https://github.com/TitoTFP/ai-factory-os.git")
    db.add_all([author, reviewer, repository])
    db.flush()
    cycle = ImprovementCycle(factory_id=factory.id, repository_id=repository.id, objective="recover", author_agent_id=author.id, reviewer_agent_id=reviewer.id, status="running", lease_until=now() - timedelta(seconds=10))
    db.add(cycle)
    db.commit()
    cycle_id = cycle.id
    db.close()

    asyncio.run(Runtime().recover_abandoned_tasks())

    db = SessionLocal()
    try:
        recovered = db.get(ImprovementCycle, cycle_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.error == "requeued after worker restart or lease expiry"
    finally:
        db.close()
