from __future__ import annotations

import sys

import pytest
from sqlalchemy import select

from app.repository import RepositoryError, read_file, run_configured_commands, safe_repository_path, write_file
from app.security import decrypt_secret
from app.db import SessionLocal
from app.models import Repository, RepositoryCredential


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
