from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.docker


def _compose_python(code: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.setdefault("SECRET_KEY", "docker-workspace-test-secret-key-32-chars")
    environment.setdefault("MASTER_KEY", "X2FHjTTGfPR5ixOXRbs5Op0PTJQfokcKdMrWYEQMoK8=")
    return subprocess.run(
        ["docker", "compose", "run", "--rm", "--no-deps", "api", "python", "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_workspace_survives_api_container_recreation_and_stays_agent_private():
    if os.getenv("RUN_DOCKER_TESTS") != "1":
        pytest.skip("set RUN_DOCKER_TESTS=1 to run container recreation checks")

    factory_id = f"container-recreation-{uuid.uuid4().hex}"
    agent_a = "agent-a"
    agent_b = "agent-b"
    first = _compose_python(
        "from app.services import safe_workspace_path, write_workspace_artifact; "
        f"write_workspace_artifact({factory_id!r}, 'persisted.txt', 'survives recreation', {agent_a!r}); "
        f"safe_workspace_path({factory_id!r}, '../{agent_a}/persisted.txt', {agent_b!r})"
    )
    assert first.returncode != 0
    assert "agent boundary" in first.stderr

    second = _compose_python(
        "from app.services import safe_workspace_path; "
        f"path = safe_workspace_path({factory_id!r}, 'persisted.txt', {agent_a!r}); "
        "assert path.read_text(encoding='utf-8') == 'survives recreation'; "
        f"other = safe_workspace_path({factory_id!r}, '.', {agent_b!r}); "
        "assert not (other / 'persisted.txt').exists(); "
        f"safe_workspace_path({factory_id!r}, '../{agent_a}/persisted.txt', {agent_b!r})"
    )
    assert second.returncode != 0
    assert "agent boundary" in second.stderr

    cleanup = _compose_python(
        "import shutil; from pathlib import Path; "
        f"shutil.rmtree(Path('/app/data/factories') / {factory_id!r}, ignore_errors=True)"
    )
    assert cleanup.returncode == 0, cleanup.stderr
