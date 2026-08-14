from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from sqlalchemy import select

from app.repository import RepositoryError, create_worktree, ensure_checkout, read_file, repository_root, run_configured_commands, safe_repository_path, write_file
from app.services import execute_repository_tool
from app.security import decrypt_secret
from app.db import SessionLocal
from app.models import Factory, ImprovementCycle, Repository, RepositoryCredential


@pytest.mark.factory_zero
def test_repository_workspace_rejects_escape_and_runs_only_argv_commands(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "README.md").write_text("Factory Zero", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read", encoding="utf-8")

    assert read_file(worktree, "README.md")["content"] == "Factory Zero"
    write_file(worktree, "src/change.txt", "verified")
    assert (worktree / "src/change.txt").read_text(encoding="utf-8") == "verified"
    with pytest.raises(RepositoryError, match="relative"):
        safe_repository_path(worktree, "../secret.txt")
    (worktree / "linked.txt").symlink_to(secret)
    with pytest.raises(RepositoryError, match="symlink"):
        safe_repository_path(worktree, "linked.txt")

    repository = Repository(
        factory_id="factory",
        owner="owner",
        name="repo",
        remote_url="https://github.com/owner/repo.git",
        test_commands=[[sys.executable, "-c", "print('ok')"]],
        build_commands=[],
        lint_commands=[],
    )
    result = run_configured_commands(repository, worktree, "test")
    assert result["passed"]
    assert result["commands"][0]["returncode"] == 0


@pytest.mark.factory_zero
def test_checkout_symlink_is_rejected_before_git_runs(tmp_path, monkeypatch):
    repository_root = tmp_path / "factories"
    monkeypatch.setattr("app.repository.REPOSITORY_ROOT", repository_root)
    checkout_parent = repository_root / "factory" / "repositories" / "repo"
    checkout_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout_parent / "checkout").symlink_to(outside, target_is_directory=True)
    repository = Repository(
        id="repo",
        factory_id="factory",
        owner="TitoTFP",
        name="ai-factory-os",
        remote_url="https://github.com/TitoTFP/ai-factory-os.git",
    )
    with pytest.raises(RepositoryError, match="cannot be a symlink"):
        ensure_checkout(repository)


@pytest.mark.factory_zero
def test_worktrees_root_symlink_is_rejected_before_git_worktree_add(tmp_path, monkeypatch):
    repository_root = tmp_path / "factories"
    monkeypatch.setattr("app.repository.REPOSITORY_ROOT", repository_root)
    repository_base = repository_root / "factory" / "repositories" / "repo"
    checkout = repository_base / "checkout"
    checkout.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository_base / "worktrees").symlink_to(outside, target_is_directory=True)

    def fake_git(args, **_kwargs):
        stdout = "b" * 40 + "\n" if args and args[0] == "rev-parse" else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.repository.ensure_checkout", lambda *_args, **_kwargs: checkout)
    monkeypatch.setattr("app.repository._run_git", fake_git)
    repository = Repository(
        id="repo",
        factory_id="factory",
        owner="TitoTFP",
        name="ai-factory-os",
        remote_url="https://github.com/TitoTFP/ai-factory-os.git",
    )
    with pytest.raises(RepositoryError, match="worktrees root"):
        create_worktree(repository, "cycle")


@pytest.mark.factory_zero

def test_generic_merge_tool_requires_successful_github_checks(database, monkeypatch):
    db = SessionLocal()
    factory = Factory(owner_id="u", name="Merge Gate", mission="m", primary_objective="o", constraints=[])
    db.add(factory)
    db.flush()
    repository = Repository(
        factory_id=factory.id,
        owner="TitoTFP",
        name="ai-factory-os",
        remote_url="https://github.com/TitoTFP/ai-factory-os.git",
    )
    db.add(repository)
    db.flush()
    credential = RepositoryCredential(
        factory_id=factory.id,
        repository_id=repository.id,
        provider="github",
        encrypted_token="encrypted",
        permissions=["merge"],
    )
    cycle = ImprovementCycle(
        factory_id=factory.id,
        repository_id=repository.id,
        objective="merge",
        phase="merge",
        head_sha="h" * 40,
        verification={"passed": True},
        review={"approved": True},
    )
    db.add_all([credential, cycle])
    db.commit()
    worktree = repository_root(factory.id, repository.id) / "worktrees" / cycle.id
    worktree.mkdir(parents=True)
    monkeypatch.setattr("app.services.repository_token_for", lambda *_args, **_kwargs: "token")

    async def failed_checks(*_args, **_kwargs):
        return {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "failure"}]}

    async def unexpected_merge(*_args, **_kwargs):
        raise AssertionError("merge must not run when checks fail")

    monkeypatch.setattr("app.services.check_runs", failed_checks)
    monkeypatch.setattr("app.services.merge_pull_request", unexpected_merge)
    with pytest.raises(PermissionError, match="successful GitHub checks"):
        asyncio.run(execute_repository_tool(
            db,
            factory,
            None,
            None,
            {"operation": "merge", "repository_id": repository.id, "worktree_path": str(worktree), "improvement_cycle_id": cycle.id, "number": 1, "head_sha": cycle.head_sha},
        ))
    db.close()


@pytest.mark.factory_zero
def test_repository_token_is_encrypted_and_never_returned(client, auth):
    factory_response = client.post(
        "/api/factories",
        headers=auth,
        json={
            "name": "Factory Zero",
            "mission": "Improve the operating system",
            "primary_objective": "Produce verified improvements",
            "provider_api_key": "provider-secret",
            "tool_permissions": ["workspace", "repository"],
        },
    )
    assert factory_response.status_code == 201
    factory_id = factory_response.json()["id"]

    rejected = client.post(
        f"/api/factories/{factory_id}/repositories",
        headers=auth,
        json={"owner": "other", "name": "repo"},
    )
    assert rejected.status_code == 422
    assert "TitoTFP/ai-factory-os" in rejected.text

    response = client.post(
        f"/api/factories/{factory_id}/repositories",
        headers=auth,
        json={
            "owner": "TitoTFP",
            "name": "ai-factory-os",
            "default_branch": "master",
            "github_token": "github-secret-token",
        },
    )
    assert response.status_code == 200
    assert "github-secret-token" not in response.text
    repository_id = response.json()["id"]

    db = SessionLocal()
    try:
        credential = db.scalar(select(RepositoryCredential).where(RepositoryCredential.repository_id == repository_id))
        assert credential is not None
        assert credential.encrypted_token != "github-secret-token"
        assert decrypt_secret(credential.encrypted_token) == "github-secret-token"
    finally:
        db.close()
