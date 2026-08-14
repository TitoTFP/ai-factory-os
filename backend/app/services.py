from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .network import safe_http_transport, validate_external_url
from .models import (
    Agent,
    Artifact,
    Event,
    Factory,
    FactoryCredential,
    FactoryRun,
    Goal,
    ImprovementCycle,
    Repository,
    RepositoryCredential,
    Message,
    Space,
    Usage,
    Task,
    Tool,
    now,
    new_id,
)
from .provider import OpenAICompatibleProvider, ProviderConfig, ProviderError, ProviderResponse
from .repository import (
    RepositoryError,
    check_runs,
    cleanup_worktree,
    create_pull_request,
    create_worktree,
    ensure_checkout,
    git_commit,
    git_diff,
    git_status,
    merge_pull_request,
    pull_request,
    push_branch,
    read_file,
    repository_root,
    run_configured_commands,
    search_files,
    safe_repository_path,
    write_file,
)
from .security import decrypt_secret


class ServiceError(RuntimeError):
    """A runtime service operation failed with a user-safe message."""

WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / "data" / "factories"


def record_event(
    db: Session,
    factory_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor_type: str = "system",
    actor_id: str = "system",
) -> Event:
    event = Event(
        factory_id=factory_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(event)
    return event


def credential_for(db: Session, factory_id: str) -> FactoryCredential | None:
    return db.scalar(
        select(FactoryCredential).where(
            FactoryCredential.factory_id == factory_id,
            FactoryCredential.provider == "openai-compatible",
        )
    )


def provider_for(db: Session, factory_id: str) -> OpenAICompatibleProvider:
    credential = credential_for(db, factory_id)
    if not credential:
        raise ProviderError("factory OpenAI-compatible credential is not configured")
    try:
        api_key = decrypt_secret(credential.encrypted_api_key)
    except (ValueError, TypeError, InvalidToken) as exc:
        raise ProviderError("factory provider credential could not be decrypted") from exc
    return OpenAICompatibleProvider(ProviderConfig(credential.base_url, credential.model, api_key))


def repository_for(db: Session, factory_id: str, repository_id: str) -> Repository:
    repository = db.scalar(select(Repository).where(Repository.id == repository_id, Repository.factory_id == factory_id))
    if not repository:
        raise RepositoryError("repository does not belong to this factory")
    return repository


def repository_token_for(db: Session, factory_id: str, repository_id: str) -> str | None:
    credential = db.scalar(
        select(RepositoryCredential).where(
            RepositoryCredential.factory_id == factory_id,
            RepositoryCredential.repository_id == repository_id,
            RepositoryCredential.provider == "github",
        )
    )
    if not credential:
        return None
    try:
        return decrypt_secret(credential.encrypted_token)
    except (ValueError, TypeError, InvalidToken) as exc:
        raise RepositoryError("repository credential could not be decrypted") from exc


def _repository_permission(operation: str) -> str:
    if operation in {"checkout", "read", "search", "status", "diff"}:
        return "read"
    if operation in {"worktree", "write", "commit", "verify", "test", "build", "lint"}:
        return "write"
    if operation in {"push", "create_pr"}:
        return "pull_request"
    if operation == "merge":
        return "merge"
    raise RepositoryError(f"unsupported repository operation: {operation}")


async def execute_repository_tool(
    db: Session,
    factory: Factory,
    agent: Agent | None,
    task: Task | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    operation = str(arguments.get("operation", "status"))
    repository_id = str(arguments.get("repository_id") or (task.inputs.get("repository_id") if task else ""))
    repository = repository_for(db, factory.id, repository_id)
    credential = db.scalar(
        select(RepositoryCredential).where(
            RepositoryCredential.factory_id == factory.id,
            RepositoryCredential.repository_id == repository.id,
            RepositoryCredential.provider == "github",
        )
    )
    required = _repository_permission(operation)
    if credential and required not in (credential.permissions or []):
        raise PermissionError(f"repository operation is not allowed: {operation}")
    if operation in {"push", "create_pr", "merge"} and not credential:
        raise RepositoryError("repository credential is required for this operation")
    token = repository_token_for(db, factory.id, repository.id)
    worktree = str(arguments.get("worktree_path") or (task.inputs.get("worktree_path") if task else ""))
    if operation == "checkout":
        checkout = ensure_checkout(repository, token)
        result = {"repository_id": repository.id, "ready": True, "path": str(checkout)}
    elif operation == "worktree":
        cycle_id = str(arguments.get("cycle_id") or (task.inputs.get("improvement_cycle_id") if task else ""))
        if not cycle_id:
            raise RepositoryError("cycle_id is required to create a worktree")
        path, branch, base_sha = create_worktree(repository, cycle_id, token)
        result = {"worktree_path": str(path), "branch": branch, "base_sha": base_sha}
    elif not worktree:
        raise RepositoryError("worktree_path is required for this repository operation")
    else:
        worktree_root = (repository_root(factory.id, repository.id) / "worktrees").resolve()
        resolved_worktree = Path(worktree).resolve()
        if worktree_root not in resolved_worktree.parents:
            raise RepositoryError("repository operation escaped the worktree boundary")
        cycle_scope = str(arguments.get("improvement_cycle_id") or (task.inputs.get("improvement_cycle_id") if task else ""))
        if cycle_scope and resolved_worktree.name != cycle_scope:
            raise RepositoryError("repository operation targeted a different improvement cycle")
        if operation == "read":
            result = read_file(worktree, str(arguments.get("path", "README.md")))
        elif operation == "search":
            result = search_files(worktree, str(arguments.get("query", "")))
        elif operation == "write":
            result = write_file(worktree, str(arguments.get("path", "")), str(arguments.get("content", "")))
        elif operation == "status":
            result = git_status(worktree)
        elif operation == "diff":
            result = git_diff(worktree)
        elif operation == "commit":
            result = {"head_sha": git_commit(worktree, str(arguments.get("message", "Factory Zero improvement")))}
        elif operation in {"verify", "test", "build", "lint"}:
            kind = str(arguments.get("kind", operation if operation in {"test", "build", "lint"} else "test"))
            result = run_configured_commands(repository, worktree, kind)
        elif operation == "push":
            branch = str(arguments.get("branch", ""))
            if not branch:
                raise RepositoryError("branch is required to push a repository worktree")
            push_branch(worktree, branch, token or "")
            result = {"branch": branch, "pushed": True}
        elif operation == "create_pr":
            branch = str(arguments.get("branch", ""))
            if not branch:
                raise RepositoryError("branch is required to create a pull request")
            result = await create_pull_request(repository, token or "", branch, str(arguments.get("title", "Factory Zero improvement")), str(arguments.get("body", "")))
        elif operation == "merge":
            cycle_id = str(arguments.get("improvement_cycle_id") or (task.inputs.get("improvement_cycle_id") if task else ""))
            cycle = db.scalar(select(ImprovementCycle).where(ImprovementCycle.id == cycle_id, ImprovementCycle.factory_id == factory.id)) if cycle_id else None
            if not cycle or cycle.phase != "merge":
                raise PermissionError("merge requires an active improvement cycle")
            if not bool(cycle.verification.get("passed")) or not bool(cycle.review.get("approved")):
                raise PermissionError("merge requires verified work and an independent approved review")
            try:
                pull_number = int(arguments.get("number", 0))
            except (TypeError, ValueError) as exc:
                raise RepositoryError("invalid pull request number") from exc
            result = await merge_pull_request(repository, token or "", pull_number, str(arguments.get("head_sha") or cycle.head_sha or ""))
        else:
            raise RepositoryError(f"unsupported repository operation: {operation}")
    record_event(
        db,
        factory.id,
        "repository_tool_called",
        {"repository_id": repository.id, "operation": operation, "result": {key: value for key, value in result.items() if key not in {"content", "diff", "output"}}},
        actor_type="agent" if agent else "system",
        actor_id=agent.id if agent else "system",
    )
    db.commit()
    return result


def _json_payload(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ProviderError("provider response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderError("provider response must be a JSON object")
    return value


def _record_usage(db: Session, factory_id: str, response: ProviderResponse, *, agent_id: str | None = None, task_id: str | None = None, request_kind: str = "agent", model: str = "") -> None:
    db.add(Usage(
        factory_id=factory_id,
        agent_id=agent_id,
        task_id=task_id,
        model=model,
        request_kind=request_kind,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        total_tokens=response.total_tokens,
        cost_usd=(response.prompt_tokens * 0.00000015) + (response.completion_tokens * 0.0000006),
    ))


async def architect_factory(db: Session, factory: Factory) -> dict[str, list[Any]]:
    existing = db.scalar(select(Space).where(Space.factory_id == factory.id))
    if existing:
        return {
            "spaces": list(db.scalars(select(Space).where(Space.factory_id == factory.id))),
            "agents": list(db.scalars(select(Agent).where(Agent.factory_id == factory.id))),
            "goals": list(db.scalars(select(Goal).where(Goal.factory_id == factory.id))),
        }
    prompt = {
        "factory": factory.name,
        "mission": factory.mission,
        "primary_objective": factory.primary_objective,
        "constraints": factory.constraints,
        "instruction": (
            "Design a small, practical AI factory. Return JSON only with arrays spaces, agents, goals. "
            "Each space has name and purpose. Each agent has name, role, space (space name), objective, "
            "responsibilities. Each goal has title, objective, criteria (array of measurable strings). "
            "Create at least 2 spaces, 3 agents, and 1 goal."
        ),
    }
    provider = provider_for(db, factory.id)
    text = await provider.chat(
        [
            {"role": "system", "content": "You are the Factory Architect. Follow the requested JSON schema."},
            {"role": "user", "content": json.dumps(prompt)},
        ],
        json_mode=True,
    )
    _record_usage(db, factory.id, provider.last_response, request_kind="architect", model=provider.config.model)
    plan = _json_payload(text)
    spaces_data = plan.get("spaces")
    agents_data = plan.get("agents")
    goals_data = plan.get("goals")
    if not isinstance(spaces_data, list) or not isinstance(agents_data, list) or not isinstance(goals_data, list):
        raise ProviderError("architect response must contain spaces, agents, and goals arrays")
    if len(spaces_data) < 2 or len(agents_data) < 3 or not goals_data:
        raise ProviderError("architect response must create at least 2 spaces, 3 agents, and 1 goal")

    spaces: list[Space] = []
    by_name: dict[str, Space] = {}
    for raw in spaces_data:
        if not isinstance(raw, dict) or not str(raw.get("name", "")).strip() or not str(raw.get("purpose", "")).strip():
            continue
        space = Space(
            factory_id=factory.id,
            name=str(raw["name"]).strip()[:160],
            purpose=str(raw.get("purpose", "")).strip(),
            shared_memory={"factory_mission": factory.mission},
        )
        db.add(space)
        spaces.append(space)
        by_name[space.name.casefold()] = space
    db.flush()
    if len(spaces) < 2:
        raise ProviderError("architect produced fewer than 2 valid spaces")

    credential = credential_for(db, factory.id)
    agent_model = credential.model if credential else settings.provider_model
    agents: list[Agent] = []
    for raw in agents_data:
        if not isinstance(raw, dict) or not str(raw.get("name", "")).strip() or not str(raw.get("role", "")).strip():
            continue
        requested_space = str(raw.get("space", "")).casefold()
        space = by_name.get(requested_space, spaces[0])
        agent = Agent(
            factory_id=factory.id,
            space_id=space.id,
            name=str(raw["name"]).strip()[:160],
            role=str(raw.get("role", "Generalist"))[:160],
            objective=str(raw.get("objective", factory.primary_objective)),
            responsibilities=raw.get("responsibilities", []) if isinstance(raw.get("responsibilities", []), list) and raw.get("responsibilities") else [],
            model=agent_model,
            system_prompt=f"You are {raw.get('name', 'an agent')}, {raw.get('role', 'a factory worker')}.",
            budget={"tokens": 0, "requests": 0},
            relationships={"can_communicate_with": []},
        )
        db.add(agent)
        agents.append(agent)
    db.flush()
    if len(agents) < 3 or any(not agent.responsibilities for agent in agents):
        raise ProviderError("architect produced fewer than 3 valid agents with responsibilities")

    goals: list[Goal] = []
    for raw in goals_data:
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        criteria = raw.get("criteria", [])
        if not isinstance(criteria, list) or not criteria or any(not str(item).strip() for item in criteria):
            continue
        goal = Goal(
            factory_id=factory.id,
            title=str(raw["title"]).strip()[:240],
            objective=str(raw.get("objective", factory.primary_objective)),
            criteria=criteria if isinstance(criteria, list) else [],
            status="pending",
        )
        db.add(goal)
        goals.append(goal)
    db.flush()
    if not goals:
        raise ProviderError("architect produced no valid goals")

    allowed_tools = set(credential.permissions if credential else [])
    db.add(
        Tool(
            factory_id=factory.id,
            name="workspace",
            description="Read and write files inside this factory workspace.",
            enabled="workspace" in allowed_tools,
            permissions=["read", "write"],
        )
    )
    db.add(
        Tool(
            factory_id=factory.id,
            name="web_fetch",
            description="Fetch public web resources and save the response as an artifact.",
            enabled="web_fetch" in allowed_tools,
            permissions=["GET"],
        )
    )
    db.add(
        Tool(
            factory_id=factory.id,
            name="http",
            description="Perform generic HTTP requests using this factory's credential scope.",
            enabled="http" in allowed_tools,
            permissions=["GET", "POST", "PUT", "DELETE"],
        )
    )
    db.add(
        Tool(
            factory_id=factory.id,
            name="repository",
            description="Inspect and modify the configured repository worktree through audited operations.",
            enabled="repository" in allowed_tools,
            permissions=["read", "write", "pull_request", "merge"],
        )
    )
    kickoff = Task(
        factory_id=factory.id,
        goal_id=goals[0].id,
        assignee_id=agents[0].id,
        title=f"Start: {goals[0].title}",
        description=goals[0].objective,
        inputs={"kind": "goal_kickoff"},
        created_by="architect",
    )
    db.add(kickoff)
    db.flush()
    factory.status = "active"
    record_event(
        db,
        factory.id,
        "factory_architected",
        {"spaces": len(spaces), "agents": len(agents), "goals": len(goals), "kickoff_task": kickoff.id},
    )
    db.commit()
    return {"spaces": spaces, "agents": agents, "goals": goals}


def _workspace_scope(factory_id: str, agent_id: str | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", factory_id):
        raise ValueError("invalid factory workspace identifier")
    scope_name = agent_id or "_system"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", scope_name):
        raise ValueError("invalid agent workspace identifier")

    base = WORKSPACE_ROOT.resolve()
    factory_path = WORKSPACE_ROOT / factory_id
    if factory_path.is_symlink():
        raise ValueError("factory workspace cannot be a symlink")
    factory_root = factory_path.resolve()
    if factory_root != base and base not in factory_root.parents:
        raise ValueError("workspace path escapes workspace boundary")

    agents_path = factory_path / "agents"
    if agents_path.is_symlink():
        raise ValueError("agent workspace directory cannot be a symlink")
    agent_path = agents_path / scope_name
    if agent_path.is_symlink():
        raise ValueError("agent workspace cannot be a symlink")
    agent_root = agent_path.resolve()
    if factory_root not in agent_root.parents:
        raise ValueError("workspace path escapes factory boundary")
    return agent_root


def safe_workspace_path(factory_id: str, relative: str, agent_id: str | None = None) -> Path:
    relative = relative.strip().lstrip("/")
    root = _workspace_scope(factory_id, agent_id)
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("workspace path escapes agent boundary")
    return candidate


def write_workspace_artifact(factory_id: str, name: str, content: str, agent_id: str | None = None) -> str:
    path = safe_workspace_path(factory_id, name, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path.relative_to(_workspace_scope(factory_id, agent_id)))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        secret_keys = {"api_key", "authorization", "token", "password", "secret", "cookie", "headers", "body"}
        return {key: "[REDACTED]" if key.casefold() in secret_keys else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _persist_tool_artifact(
    db: Session,
    factory: Factory,
    agent: Agent | None,
    task: Task | None,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> Artifact | None:
    if tool_name == "workspace" and arguments.get("operation", "read") != "write":
        return None
    if tool_name == "workspace":
        name = str(arguments.get("path", "artifact.txt"))
        content = str(arguments.get("content", ""))
        uri = f"workspace://{agent.id if agent else '_system'}/{name.lstrip('/')}"
        kind = "file"
    elif tool_name in {"web_fetch", "http"}:
        name = str(arguments.get("artifact_name") or f"{tool_name}-{task.id if task else new_id()}.txt")[:240]
        content = str(result.get("body", ""))
        uri = str(result.get("url", arguments.get("url", "")))
        kind = "http"
    else:
        return None
    existing = db.scalar(select(Artifact).where(Artifact.factory_id == factory.id, Artifact.task_id == (task.id if task else None), Artifact.name == name)) if task else None
    if existing:
        return existing
    artifact = Artifact(
        factory_id=factory.id,
        space_id=agent.space_id if agent else None,
        agent_id=agent.id if agent else None,
        task_id=task.id if task else None,
        name=name,
        kind=kind,
        content=content,
        uri=uri,
        extra={"tool": tool_name, "arguments": _redact(arguments)},
    )
    db.add(artifact)
    db.flush()
    result["artifact_id"] = artifact.id
    result["artifact_uri"] = artifact.uri
    return artifact


async def execute_tool(
    db: Session,
    factory: Factory,
    agent: Agent | None,
    task: Task | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    tool = db.scalar(select(Tool).where(Tool.factory_id == factory.id, Tool.name == tool_name, Tool.enabled.is_(True)))
    if not tool:
        raise ValueError(f"tool is not enabled: {tool_name}")
    actor_id = agent.id if agent else "system"
    if tool_name == "workspace":
        operation = arguments.get("operation", "read")
        if operation not in tool.permissions:
            raise PermissionError(f"workspace operation is not allowed: {operation}")
        name = str(arguments.get("path", "artifact.txt"))
        workspace_agent_id = agent.id if agent else None
        root = safe_workspace_path(factory.id, ".", workspace_agent_id)
        root.mkdir(parents=True, exist_ok=True)
        path = safe_workspace_path(factory.id, name, workspace_agent_id)
        if operation == "write":
            content = str(arguments.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            result = {"path": name, "bytes": len(content.encode())}
        elif path.is_dir():
            result = {"path": name, "entries": sorted(str(item.relative_to(root)) for item in path.rglob("*"))}
        elif path.is_file():
            result = {"path": name, "content": path.read_text(encoding="utf-8")}
        else:
            result = {"path": name, "content": "", "exists": False}
    elif tool_name in {"web_fetch", "http"}:
        method = "GET" if tool_name == "web_fetch" else str(arguments.get("method", "GET")).upper()
        if method not in tool.permissions:
            raise PermissionError(f"HTTP method is not allowed for {tool_name}: {method}")
        url = str(arguments.get("url", ""))
        transport = safe_http_transport(url)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False, transport=transport) as client:
            response = await client.request(method, url, json=arguments.get("body"))
        result = {"status_code": response.status_code, "url": str(response.url), "body": response.text[:200_000]}
    elif tool_name == "repository":
        result = await execute_repository_tool(db, factory, agent, task, arguments)
    else:
        raise ValueError(f"unknown tool: {tool_name}")
    _persist_tool_artifact(db, factory, agent, task, tool_name, arguments, result)
    record_event(
        db,
        factory.id,
        "tool_called",
        {"tool": tool_name, "arguments": _redact(arguments), "result": {"status": result.get("status_code", "ok")}},
        actor_type="agent" if agent else "system",
        actor_id=actor_id,
    )
    db.commit()
    return result


class Runtime:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def recover_abandoned_tasks(self) -> None:
        db = SessionLocal()
        try:
            current_time = now()
            stale_before = current_time - timedelta(seconds=settings.task_lease_seconds)
            stale_tasks = list(db.scalars(select(Task).where(
                Task.status == "running",
                or_(Task.lease_until < current_time, and_(Task.lease_until.is_(None), Task.updated_at < stale_before)),
            )))
            stale_task_ids = [task.id for task in stale_tasks]
            for task in stale_tasks:
                task.status = "queued"
                task.lease_until = None
                task.error = "requeued after worker restart or lease expiry"
                record_event(db, task.factory_id, "task_recovered", {"task_id": task.id})
            if stale_tasks:
                for agent in db.scalars(select(Agent).where(Agent.current_task_id.in_(stale_task_ids))):
                    agent.status = "idle"
                    agent.current_task_id = None
                db.commit()
            stale_cycles = list(db.scalars(select(ImprovementCycle).where(
                ImprovementCycle.status == "running",
                or_(ImprovementCycle.lease_until < current_time, and_(ImprovementCycle.lease_until.is_(None), ImprovementCycle.updated_at < stale_before)),
            )))
            for cycle in stale_cycles:
                cycle.status = "queued"
                cycle.lease_until = None
                cycle.error = "requeued after worker restart or lease expiry"
                record_event(db, cycle.factory_id, "improvement_cycle_recovered", {"cycle_id": cycle.id, "phase": cycle.phase})
            if stale_cycles:
                db.commit()
            orphaned_agents = list(db.scalars(select(Agent).where(Agent.status == "working", Agent.current_task_id.is_not(None))))
            for agent in orphaned_agents:
                current_task = db.get(Task, agent.current_task_id)
                if current_task is None or current_task.status != "running":
                    agent.status = "idle"
                    agent.current_task_id = None
            if orphaned_agents:
                db.commit()
            db.commit()
        finally:
            db.close()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        # Recover before discovery so stale work is repaired even when its
        # previous run was stopped and therefore is not in the worker query.
        await self.recover_abandoned_tasks()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="factory-runtime")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _record_runtime_error(self, factory_id: str, exc: Exception) -> None:
        db = SessionLocal()
        try:
            runs = list(db.scalars(select(FactoryRun).where(FactoryRun.factory_id == factory_id, FactoryRun.status == "running")))
            message = str(exc)[:2000]
            for run in runs:
                run.last_error = message
                record_event(db, factory_id, "runtime_error", {"run_id": run.id, "error": message})
            if runs:
                db.commit()
        finally:
            db.close()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                db = SessionLocal()
                try:
                    factory_ids = list(db.scalars(select(FactoryRun.factory_id).where(FactoryRun.status == "running")))
                finally:
                    db.close()
                for factory_id in factory_ids:
                    try:
                        await self.process_factory(factory_id)
                    except Exception as exc:
                        await self._record_runtime_error(factory_id, exc)
            except Exception as exc:
                # A database discovery failure must not kill the daemon; the next
                # poll retries it. Per-factory failures are persisted above.
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.runtime_poll_seconds)
            except TimeoutError:
                await asyncio.sleep(0)

    async def process_factory(self, factory_id: str) -> None:
        db = SessionLocal()
        try:
            factory = db.get(Factory, factory_id)
            run = db.scalar(select(FactoryRun).where(FactoryRun.factory_id == factory_id, FactoryRun.status == "running"))
            if not factory or not run:
                return
            await self._process_inbox(db, factory)
            current_time = now()
            stale_before = current_time - timedelta(seconds=settings.task_lease_seconds)
            stale_tasks = list(db.scalars(select(Task).where(
                Task.factory_id == factory_id,
                Task.status == "running",
                or_(Task.lease_until < current_time, and_(Task.lease_until.is_(None), Task.updated_at < stale_before)),
            )))
            for stale_task in stale_tasks:
                stale_task.status = "queued"
                stale_task.lease_until = None
                stale_task.error = "requeued after worker restart or lease expiry"
                record_event(db, factory_id, "task_recovered", {"task_id": stale_task.id})
            if stale_tasks:
                stale_task_ids = [stale_task.id for stale_task in stale_tasks]
                for agent in db.scalars(select(Agent).where(Agent.current_task_id.in_(stale_task_ids))):
                    agent.status = "idle"
                    agent.current_task_id = None
                db.commit()
            task_query = (
                select(Task)
                .where(Task.factory_id == factory_id, Task.status == "queued", Task.available_at <= current_time)
                .order_by(Task.created_at)
                .with_for_update(skip_locked=True)
            )
            task = db.scalar(task_query)
            if task is not None:
                task.lease_until = current_time + timedelta(seconds=settings.task_lease_seconds)
                db.commit()
            if task is not None:
                await self._process_task(db, factory, task)
            elif await self._process_improvement_cycle(db, factory):
                return
            else:
                await self._evaluate(db, factory, run)
        finally:
            db.close()

    async def _process_inbox(self, db: Session, factory: Factory) -> None:
        """Turn typed agent inbox messages into durable work exactly once."""
        messages = list(db.scalars(select(Message).where(
            Message.factory_id == factory.id,
            Message.recipient_agent_id.is_not(None),
            Message.status.in_(["queued", "delivered"]),
            Message.read_at.is_(None),
        ).order_by(Message.created_at).limit(50)))
        for message in messages:
            recipient = db.scalar(select(Agent).where(
                Agent.id == message.recipient_agent_id,
                Agent.factory_id == factory.id,
            ))
            if recipient is None:
                message.status = "failed"
                message.read_at = now()
                message.payload = {
                    **(message.payload if isinstance(message.payload, dict) else {}),
                    "error": "recipient agent does not belong to message factory",
                }
                record_event(
                    db,
                    factory.id,
                    "message_rejected",
                    {
                        "message_id": message.id,
                        "recipient_agent_id": message.recipient_agent_id,
                        "reason": "cross_factory_recipient",
                    },
                )
                continue
            if message.message_type in {"TASK_REQUEST", "REVIEW_REQUEST"}:
                payload = message.payload if isinstance(message.payload, dict) else {}
                task_id = payload.get("task_id")
                # TASK_REQUEST.task_id is an idempotency hint for an existing
                # delegated task. REVIEW_REQUEST.task_id identifies the source
                # task and must still create a distinct review task.
                existing = db.scalar(select(Task).where(Task.id == task_id, Task.factory_id == factory.id)) if task_id and message.message_type == "TASK_REQUEST" else None
                if not existing:
                    goal_id = payload.get("goal_id")
                    if goal_id and not db.scalar(select(Goal).where(Goal.id == goal_id, Goal.factory_id == factory.id)):
                        goal_id = None
                    parent_id = payload.get("parent_id")
                    if parent_id and not db.scalar(select(Task).where(Task.id == parent_id, Task.factory_id == factory.id)):
                        parent_id = None
                    description = message.body
                    if message.message_type == "REVIEW_REQUEST":
                        artifact_id = payload.get("artifact_id")
                        artifact = db.scalar(select(Artifact).where(Artifact.id == artifact_id, Artifact.factory_id == factory.id))
                        if not artifact:
                            message.status = "failed"
                            message.read_at = now()
                            continue
                        description = f"Review artifact {artifact.name} ({artifact.id}).\n{message.body}\nArtifact content:\n{artifact.content}"
                    inputs = dict(payload.get("inputs", {}) if isinstance(payload.get("inputs", {}), dict) else {})
                    if message.message_type == "REVIEW_REQUEST":
                        inputs["review_artifact_id"] = payload.get("artifact_id")
                    if message.sender_agent_id:
                        inputs["reply_to_agent_id"] = message.sender_agent_id
                    inputs["request_message_id"] = message.id
                    task = Task(
                        factory_id=factory.id,
                        goal_id=goal_id,
                        parent_id=parent_id,
                        assignee_id=message.recipient_agent_id,
                        title=message.subject or ("Artifact review" if message.message_type == "REVIEW_REQUEST" else "Delegated task"),
                        description=description,
                        inputs=inputs,
                        created_by=message.sender_agent_id or "agent",
                    )
                    db.add(task)
                    db.flush()
                    message.payload = {**payload, "task_id": task.id}
                    record_event(
                        db,
                        factory.id,
                        "artifact_review_task_created" if message.message_type == "REVIEW_REQUEST" else "task_delegated",
                        {
                            "task_id": task.id,
                            "message_id": message.id,
                            "correlation_id": message.correlation_id,
                            "sender_agent_id": message.sender_agent_id,
                            "recipient_agent_id": message.recipient_agent_id,
                            **({"artifact_id": payload.get("artifact_id")} if message.message_type == "REVIEW_REQUEST" else {}),
                        },
                    )
            message.status = "read"
            message.delivered_at = message.delivered_at or now()
            message.read_at = now()
        if messages:
            db.commit()

    @staticmethod
    def _agent_tools(db: Session, factory_id: str) -> list[dict[str, Any]]:
        tools = []
        for tool in db.scalars(select(Tool).where(Tool.factory_id == factory_id, Tool.enabled.is_(True))):
            if tool.name == "workspace":
                operations = [operation for operation in ("read", "write") if operation in tool.permissions]
                if not operations:
                    continue
                properties = {"operation": {"type": "string", "enum": operations}, "path": {"type": "string"}, "content": {"type": "string"}}
            elif tool.name == "web_fetch":
                methods = ["GET"] if "GET" in tool.permissions else []
                if not methods:
                    continue
                properties = {"url": {"type": "string"}, "artifact_name": {"type": "string"}}
            elif tool.name == "repository":
                operations = [
                    operation for operation in (
                        "checkout", "worktree", "read", "search", "write", "status", "diff",
                        "commit", "test", "build", "lint", "push", "create_pr", "merge",
                    ) if _repository_permission(operation) in tool.permissions
                ]
                if not operations:
                    continue
                properties = {
                    "operation": {"type": "string", "enum": operations},
                    "repository_id": {"type": "string"},
                    "worktree_path": {"type": "string"},
                    "cycle_id": {"type": "string"},
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "content": {"type": "string"},
                    "kind": {"type": "string", "enum": ["test", "build", "lint"]},
                    "message": {"type": "string"},
                    "branch": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "number": {"type": "integer"},
                    "head_sha": {"type": "string"},
                    "improvement_cycle_id": {"type": "string"},
                }
            else:
                methods = [method for method in ("GET", "POST", "PUT", "DELETE") if method in tool.permissions]
                if not methods:
                    continue
                properties = {"url": {"type": "string"}, "method": {"type": "string", "enum": methods}, "body": {"type": "object"}, "artifact_name": {"type": "string"}}
            tools.append({"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": {"type": "object", "properties": properties}}})
        tools.extend([
            {"type": "function", "function": {"name": "delegate_task", "description": "Delegate a task to another agent.", "parameters": {"type": "object", "properties": {"agent_id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}, "goal_id": {"type": "string"}, "inputs": {"type": "object"}}, "required": ["agent_id", "title", "description"]}}},
            {"type": "function", "function": {"name": "request_review", "description": "Ask another agent to review an artifact.", "parameters": {"type": "object", "properties": {"agent_id": {"type": "string"}, "artifact_id": {"type": "string"}, "task_id": {"type": "string"}, "instructions": {"type": "string"}}, "required": ["agent_id", "artifact_id"]}}},
            {"type": "function", "function": {"name": "reorganize", "description": "Apply a durable organization change when workload requires it.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["hire", "hibernate", "merge", "move", "move_responsibility"]}, "agent_id": {"type": "string"}, "target_agent_id": {"type": "string"}, "space_id": {"type": "string"}}, "required": ["action"]}}},
        ])
        return tools

    async def _model_action(self, db: Session, factory: Factory, agent: Agent | None, task: Task, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name in {"workspace", "web_fetch", "http", "repository"}:
            return await execute_tool(db, factory, agent, task, name, arguments)
        if name == "delegate_task":
            target = db.scalar(select(Agent).where(Agent.id == arguments.get("agent_id"), Agent.factory_id == factory.id, Agent.status != "hibernated"))
            if not target:
                raise ValueError("delegation target is not available")
            message = Message(factory_id=factory.id, sender_agent_id=agent.id if agent else None, recipient_agent_id=target.id, message_type="TASK_REQUEST", subject=str(arguments.get("title", "Delegated task"))[:240], body=str(arguments.get("description", "")), payload={"goal_id": arguments.get("goal_id"), "parent_id": task.id, "inputs": arguments.get("inputs", {})}, correlation_id=task.id, status="delivered", delivered_at=now())
            db.add(message)
            db.flush()
            record_event(
                db,
                factory.id,
                "agent_delegated_task",
                {
                    "task_id": task.id,
                    "message_id": message.id,
                    "correlation_id": message.correlation_id,
                    "sender_agent_id": agent.id if agent else None,
                    "recipient_agent_id": target.id,
                },
                actor_type="agent" if agent else "system",
                actor_id=agent.id if agent else "system",
            )
            return {"message_id": message.id, "recipient_agent_id": target.id}
        if name == "request_review":
            artifact = db.scalar(select(Artifact).where(Artifact.id == arguments.get("artifact_id"), Artifact.factory_id == factory.id))
            target = db.scalar(select(Agent).where(Agent.id == arguments.get("agent_id"), Agent.factory_id == factory.id, Agent.status != "hibernated"))
            if not artifact or not target:
                raise ValueError("review artifact or agent is not available")
            message = Message(factory_id=factory.id, sender_agent_id=agent.id if agent else None, recipient_agent_id=target.id, message_type="REVIEW_REQUEST", subject="Review artifact", body=str(arguments.get("instructions", "Review the artifact and report findings.")), payload={"artifact_id": artifact.id, "task_id": arguments.get("task_id") or task.id}, correlation_id=task.id, status="delivered", delivered_at=now())
            db.add(message)
            db.flush()
            record_event(
                db,
                factory.id,
                "artifact_review_requested",
                {
                    "task_id": task.id,
                    "artifact_id": artifact.id,
                    "message_id": message.id,
                    "correlation_id": message.correlation_id,
                    "sender_agent_id": agent.id if agent else None,
                    "recipient_agent_id": target.id,
                },
                actor_type="agent" if agent else "system",
                actor_id=agent.id if agent else "system",
            )
            return {"message_id": message.id, "artifact_id": artifact.id}
        if name == "reorganize":
            await self.reorganize(factory.id, str(arguments.get("action", "")), agent_id=arguments.get("agent_id") or (agent.id if agent else None), target_agent_id=arguments.get("target_agent_id"), space_id=arguments.get("space_id"))
            return {"action": arguments.get("action")}
        raise ValueError(f"unknown model action: {name}")

    @staticmethod
    def _cycle_tool_schema(write: bool) -> list[dict[str, Any]]:
        operations = ["read", "search", "status", "diff"] + (["write"] if write else [])
        return [{
            "type": "function",
            "function": {
                "name": "repository",
                "description": "Use only the isolated Factory Zero worktree. Read/search/status/diff are always available; write is available only during implementation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": operations},
                        "path": {"type": "string"},
                        "query": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["operation"],
                },
            },
        }]

    async def _cycle_model_call(
        self,
        db: Session,
        factory: Factory,
        cycle: ImprovementCycle,
        agent: Agent,
        instruction: str,
        *,
        write: bool = False,
        json_mode: bool = False,
    ) -> str:
        provider = provider_for(db, factory.id)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    f"You are {agent.name}, the Factory Zero {agent.role}. Work only on the configured AI Factory OS repository. "
                    f"The isolated worktree is {cycle.worktree_path}. Repository id: {cycle.repository_id}. Cycle id: {cycle.id}. "
                    "Never invent verification evidence. Keep changes small and explain the exact files and commands used."
                ),
            },
            {"role": "user", "content": instruction},
        ]
        tools = self._cycle_tool_schema(write)
        for _ in range(8):
            try:
                text = await provider.chat(messages, json_mode=json_mode and not tools, tools=tools or None)
            except TypeError as exc:
                if "tools" not in str(exc):
                    raise
                text = await provider.chat(messages, json_mode=json_mode)
            response = provider.last_response
            _record_usage(db, factory.id, response, agent_id=agent.id, model=provider.config.model, request_kind=f"factory_zero_{cycle.phase}")
            if not response.tool_calls:
                return text
            messages.append({
                "role": "assistant",
                "content": response.content or None,
                "tool_calls": [
                    {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}
                    for call in response.tool_calls
                ],
            })
            for call in response.tool_calls:
                if call.name != "repository":
                    raise ServiceError("Factory Zero model returned an unsupported tool")
                arguments = {**call.arguments, "repository_id": cycle.repository_id, "worktree_path": cycle.worktree_path, "improvement_cycle_id": cycle.id}
                if not write and arguments.get("operation") == "write":
                    raise PermissionError("repository writes are only allowed during the implementation phase")
                result = await execute_repository_tool(db, factory, agent, None, arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)[:200_000]})
        raise ServiceError("Factory Zero provider exceeded the tool-call turn limit")

    @staticmethod
    def _cycle_json(text: str, fallback_key: str = "summary") -> dict[str, Any]:
        try:
            return _json_payload(text)
        except ProviderError:
            return {fallback_key: text[:20_000]}

    async def _process_improvement_cycle(self, db: Session, factory: Factory) -> bool:
        current_time = now()
        cycle = db.scalar(
            select(ImprovementCycle).where(
                ImprovementCycle.factory_id == factory.id,
                ImprovementCycle.status == "queued",
            ).order_by(ImprovementCycle.created_at).with_for_update(skip_locked=True)
        )
        if cycle is None:
            cycle = db.scalar(
                select(ImprovementCycle).where(
                    ImprovementCycle.factory_id == factory.id,
                    ImprovementCycle.status == "running",
                    ImprovementCycle.lease_until < current_time,
                ).order_by(ImprovementCycle.created_at).with_for_update(skip_locked=True)
            )
        if cycle is None:
            return False
        cycle.status = "running"
        cycle.lease_until = current_time + timedelta(seconds=settings.task_lease_seconds)
        cycle.started_at = cycle.started_at or current_time
        record_event(db, factory.id, "improvement_cycle_started", {"cycle_id": cycle.id, "phase": cycle.phase})
        db.commit()
        try:
            await self._advance_improvement_cycle(db, factory, cycle.id)
        except Exception as exc:
            db.rollback()
            failed = db.get(ImprovementCycle, cycle.id)
            if failed is not None:
                failed.retry_count += 1
                failed.error = str(exc)[:2_000]
                failed.lease_until = None
                if failed.retry_count <= 3:
                    failed.status = "queued"
                    record_event(db, factory.id, "improvement_cycle_retry", {"cycle_id": failed.id, "phase": failed.phase, "error": failed.error, "retry_count": failed.retry_count})
                else:
                    failed.status = "failed"
                    record_event(db, factory.id, "improvement_cycle_failed", {"cycle_id": failed.id, "phase": failed.phase, "error": failed.error, "retry_count": failed.retry_count})
                db.commit()
        return True

    async def _advance_improvement_cycle(self, db: Session, factory: Factory, cycle_id: str) -> None:
        cycle = db.get(ImprovementCycle, cycle_id)
        if cycle is None or cycle.status != "running":
            return
        repository = repository_for(db, factory.id, cycle.repository_id)
        token = repository_token_for(db, factory.id, repository.id)
        if cycle.phase == "discover":
            if not cycle.worktree_path:
                path, branch, base_sha = create_worktree(repository, cycle.id, token)
                cycle.worktree_path = str(path)
                cycle.branch_name = branch
                cycle.base_sha = base_sha
            author = db.get(Agent, cycle.author_agent_id)
            if not author or author.factory_id != factory.id:
                raise ServiceError("Factory Zero author agent is unavailable")
            text = await self._cycle_model_call(
                db,
                factory,
                cycle,
                author,
                "Return JSON with problem, rationale, files_to_inspect, and proposed_change. Inspect the repository first; do not edit files yet. Objective:\n" + cycle.objective,
            )
            cycle.proposal = self._cycle_json(text)
            cycle.phase = "implement"
            cycle.error = None
            record_event(db, factory.id, "improvement_cycle_diagnosed", {"cycle_id": cycle.id, "branch": cycle.branch_name, "base_sha": cycle.base_sha})
        elif cycle.phase == "implement":
            author = db.get(Agent, cycle.author_agent_id)
            if not author or author.factory_id != factory.id:
                raise ServiceError("Factory Zero author agent is unavailable")
            feedback = cycle.error or "none"
            text = await self._cycle_model_call(
                db,
                factory,
                cycle,
                author,
                "Implement the objective in the isolated worktree. Use repository write operations, inspect the diff, and return JSON with summary, files_changed, and commit_message. Do not commit or push yet.\nObjective:\n"
                + cycle.objective + "\nPrior proposal:\n" + json.dumps(cycle.proposal, ensure_ascii=False)[:30_000] + "\nPrevious verification/review feedback:\n" + feedback,
                write=True,
            )
            cycle.proposal = {**cycle.proposal, "implementation": self._cycle_json(text)}
            cycle.phase = "verify"
            cycle.error = None
            record_event(db, factory.id, "improvement_cycle_implemented", {"cycle_id": cycle.id, "branch": cycle.branch_name})
        elif cycle.phase == "verify":
            test_result = run_configured_commands(repository, cycle.worktree_path or "", "test")
            build_result = run_configured_commands(repository, cycle.worktree_path or "", "build")
            lint_result = run_configured_commands(repository, cycle.worktree_path or "", "lint")
            required = [test_result]
            optional = [build_result, lint_result]
            passed = bool(test_result.get("commands")) and all(bool(result.get("passed")) for result in required + [result for result in optional if result.get("commands")])
            cycle.verification = {"passed": passed, "test": test_result, "build": build_result, "lint": lint_result, "head_sha": git_status(cycle.worktree_path or "").get("diff_stat", "")}
            if passed:
                cycle.phase = "review"
                cycle.error = None
                record_event(db, factory.id, "improvement_cycle_verified", {"cycle_id": cycle.id, "passed": True})
            else:
                cycle.phase = "implement"
                cycle.error = "verification failed: " + json.dumps(cycle.verification, ensure_ascii=False)[:1_500]
                record_event(db, factory.id, "improvement_cycle_verification_failed", {"cycle_id": cycle.id, "passed": False})
        elif cycle.phase == "review":
            reviewer = db.get(Agent, cycle.reviewer_agent_id)
            author = db.get(Agent, cycle.author_agent_id)
            if not reviewer or not author or reviewer.factory_id != factory.id or reviewer.id == author.id:
                raise ServiceError("Factory Zero requires an independent reviewer")
            diff = git_diff(cycle.worktree_path or "").get("diff", "")
            text = await self._cycle_model_call(
                db,
                factory,
                cycle,
                reviewer,
                "Review the exact uncommitted patch below. Return JSON with approved (boolean), summary, findings, and risks. Approve only when the objective is met and the verification evidence is credible.\nObjective:\n"
                + cycle.objective + "\nVerification:\n" + json.dumps(cycle.verification, ensure_ascii=False)[:20_000] + "\nPatch:\n" + diff[:120_000],
                json_mode=True,
            )
            review = self._cycle_json(text)
            review["approved"] = bool(review.get("approved"))
            review["reviewer_agent_id"] = reviewer.id
            cycle.review = review
            if review["approved"]:
                cycle.phase = "merge"
                cycle.error = None
                record_event(db, factory.id, "improvement_cycle_reviewed", {"cycle_id": cycle.id, "approved": True, "reviewer_agent_id": reviewer.id})
            else:
                cycle.phase = "implement"
                cycle.error = "independent review rejected: " + str(review.get("summary", "review did not approve"))[:1_500]
                record_event(db, factory.id, "improvement_cycle_reviewed", {"cycle_id": cycle.id, "approved": False, "reviewer_agent_id": reviewer.id})
        elif cycle.phase == "merge":
            if not token:
                raise RepositoryError("GitHub credential is required for autonomous merge")
            if not bool(cycle.verification.get("passed")) or not bool(cycle.review.get("approved")):
                raise PermissionError("merge requires verification and independent review")
            cycle.head_sha = git_commit(
                cycle.worktree_path or "",
                str(cycle.proposal.get("implementation", {}).get("commit_message", "Factory Zero improvement")),
            )
            push_branch(cycle.worktree_path or "", cycle.branch_name or "", token)
            pr = await create_pull_request(repository, token, cycle.branch_name or "", f"Factory Zero: {cycle.objective[:180]}", json.dumps({"objective": cycle.objective, "proposal": cycle.proposal, "verification": cycle.verification, "review": cycle.review}, ensure_ascii=False)[:20_000])
            try:
                cycle.pr_number = int(pr.get("number", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise RepositoryError("GitHub returned an invalid pull request number") from exc
            cycle.pr_url = str(pr.get("html_url", ""))
            cycle.head_sha = str(pr.get("head", {}).get("sha", "") or "")
            if not cycle.pr_number or not cycle.head_sha:
                raise RepositoryError("GitHub did not return a pull request number and head SHA")
            merge = await merge_pull_request(repository, token, cycle.pr_number, cycle.head_sha or "")
            if not bool(merge.get("merged")):
                raise RepositoryError(str(merge.get("message", "GitHub did not merge the pull request")))
            cycle.phase = "observe"
            cycle.error = None
            record_event(db, factory.id, "improvement_cycle_merged", {"cycle_id": cycle.id, "pr_number": cycle.pr_number, "pr_url": cycle.pr_url, "head_sha": cycle.head_sha})
        elif cycle.phase == "observe":
            if not token or not cycle.pr_number:
                raise RepositoryError("merged cycle is missing GitHub reconciliation data")
            pr = await pull_request(repository, token, cycle.pr_number)
            checks = await check_runs(repository, token, cycle.head_sha or str(pr.get("merge_commit_sha", "")))
            cycle.observation = {
                "merged": bool(pr.get("merged")),
                "state": pr.get("state"),
                "merge_commit_sha": pr.get("merge_commit_sha"),
                "check_runs": {
                    "total": checks.get("total_count", 0),
                    "conclusion": [item.get("conclusion") for item in checks.get("check_runs", []) if isinstance(item, dict)],
                },
            }
            cleanup_worktree(repository, cycle.worktree_path or "", cycle.branch_name, token)
            cycle.status = "completed"
            cycle.phase = "completed"
            cycle.lease_until = None
            cycle.completed_at = now()
            cycle.error = None
            record_event(db, factory.id, "improvement_cycle_completed", {"cycle_id": cycle.id, "pr_number": cycle.pr_number, "merge_commit_sha": cycle.observation.get("merge_commit_sha")})
        cycle.updated_at = now()
        cycle.lease_until = None if cycle.status != "running" else cycle.lease_until
        db.commit()

    async def _process_task(self, db: Session, factory: Factory, task: Task) -> None:
        agent = db.scalar(select(Agent).where(Agent.id == task.assignee_id, Agent.factory_id == factory.id, Agent.status != "hibernated")) if task.assignee_id else db.scalar(select(Agent).where(Agent.factory_id == factory.id, Agent.status != "hibernated"))
        task.status = "running"
        task.lease_until = now() + timedelta(seconds=settings.task_lease_seconds)
        if agent:
            agent.status = "working"
            agent.current_task_id = task.id
        record_event(db, factory.id, "task_started", {"task_id": task.id}, actor_type="agent" if agent else "system", actor_id=agent.id if agent else "system")
        db.commit()
        try:
            if task.inputs.get("tool"):
                tool_result = await execute_tool(db, factory, agent, task, str(task.inputs["tool"]), task.inputs)
            else:
                if agent and agent.budget.get("max_tokens") and int(agent.budget.get("used_tokens", 0)) >= int(agent.budget["max_tokens"]):
                    raise ServiceError("agent token budget exhausted")
                provider = provider_for(db, factory.id)
                messages = [
                    {"role": "system", "content": (agent.system_prompt if agent else "You are a factory worker.") + " Use tools when useful; return concise results."},
                    {"role": "user", "content": f"Task: {task.title}\n{task.description}"},
                ]
                tools = self._agent_tools(db, factory.id)
                try:
                    text = await provider.chat(messages, tools=tools)
                except TypeError as exc:
                    # Keep simple test doubles and older OpenAI-compatible adapters
                    # usable while production adapters receive the tool schema.
                    if "tools" not in str(exc):
                        raise
                    text = await provider.chat(messages)
                response = provider.last_response
                if not response.content and text:
                    response = ProviderResponse(
                        content=text,
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        total_tokens=response.total_tokens,
                        tool_calls=response.tool_calls,
                    )
                _record_usage(db, factory.id, response, agent_id=agent.id if agent else None, task_id=task.id, model=provider.config.model)
                tool_results = []
                for call in response.tool_calls:
                    tool_results.append({"tool": call.name, "result": await self._model_action(db, factory, agent, task, call.name, call.arguments)})
                # A provider may return a tool call without assistant prose. Keep the
                # tool output as durable evidence instead of producing an empty artifact.
                if not text and tool_results:
                    text = json.dumps(tool_results, ensure_ascii=False)
                if agent:
                    agent.budget = {**agent.budget, "used_tokens": int(agent.budget.get("used_tokens", 0)) + response.total_tokens, "requests": int(agent.budget.get("requests", 0)) + 1}
                tool_result = {"content": text, "tool_calls": tool_results} if tool_results else {"content": text}
            filename = f"{task.id}.md"
            artifact_content = json.dumps(tool_result, ensure_ascii=False, indent=2) if not isinstance(tool_result.get("content"), str) else tool_result["content"]
            relative = write_workspace_artifact(factory.id, filename, artifact_content, agent.id if agent else None)
            review_artifact_id = task.inputs.get("review_artifact_id")
            artifact = Artifact(
                factory_id=factory.id,
                space_id=agent.space_id if agent else None,
                agent_id=agent.id if agent else None,
                task_id=task.id,
                name=filename,
                kind="review" if review_artifact_id else "text",
                content=artifact_content,
                uri=f"workspace://{agent.id if agent else '_system'}/{relative}",
                extra={
                    "tool": task.inputs.get("tool", "llm"),
                    **({"review_artifact_id": review_artifact_id} if review_artifact_id else {}),
                },
            )
            db.add(artifact)
            db.flush()
            task.outputs = {"artifact_id": artifact.id, "uri": artifact.uri, "result": tool_result}
            task.status = "done"
            task.lease_until = None
            if agent:
                agent.status = "idle"
                agent.current_task_id = None
            reply_to = task.inputs.get("reply_to_agent_id")
            recipient = db.scalar(select(Agent).where(Agent.id == reply_to, Agent.factory_id == factory.id)) if reply_to else None
            reply_type = "REVIEW_RESULT" if task.inputs.get("review_artifact_id") else "TASK_RESULT"
            message = Message(
                factory_id=factory.id,
                sender_agent_id=agent.id if agent else None,
                recipient_agent_id=recipient.id if recipient else None,
                message_type=reply_type,
                subject=task.title,
                body=f"Completed task and created {filename}.",
                payload={"task_id": task.id, "artifact_id": artifact.id, "review_artifact_id": task.inputs.get("review_artifact_id")},
                correlation_id=task.inputs.get("request_message_id") or task.id,
                status="delivered" if recipient else "queued",
                delivered_at=now() if recipient else None,
            )
            db.add(message)
            db.flush()
            record_event(db, factory.id, "task_completed", {"task_id": task.id, "artifact_id": artifact.id, "message_id": message.id}, actor_type="agent" if agent else "system", actor_id=agent.id if agent else "system")
            db.commit()
            await self._maybe_reorganize(factory.id)
        except Exception as exc:
            db.rollback()
            recovered_task = db.get(Task, task.id)
            if recovered_task is None:
                return
            task = recovered_task
            task.retry_count += 1
            task.lease_until = None
            task.error = str(exc)
            if task.retry_count <= task.max_retries:
                task.status = "queued"
                task.available_at = now() + timedelta(seconds=2**task.retry_count)
            else:
                task.status = "failed"
            agent = db.get(Agent, task.assignee_id) if task.assignee_id else None
            if agent:
                agent.status = "blocked" if task.status == "failed" else "idle"
                agent.current_task_id = None
            record_event(db, factory.id, "task_failed", {"task_id": task.id, "error": str(exc), "retry_count": task.retry_count})
            if task.status == "failed":
                db.add(Message(
                    factory_id=factory.id,
                    sender_agent_id=agent.id if agent else None,
                    message_type="ESCALATION",
                    subject=f"Task failed: {task.title}",
                    body=task.error,
                    payload={"task_id": task.id, "retry_count": task.retry_count},
                    correlation_id=task.id,
                    status="queued",
                ))
                record_event(db, factory.id, "task_escalated", {"task_id": task.id})
            db.commit()

    async def _maybe_reorganize(self, factory_id: str) -> None:
        db = SessionLocal()
        try:
            queued = db.scalar(select(func.count(Task.id)).where(Task.factory_id == factory_id, Task.status == "queued")) or 0
            if queued < 4:
                return
            factory = db.get(Factory, factory_id)
            space = db.scalar(select(Space).where(Space.factory_id == factory_id).order_by(Space.created_at))
            if not factory or not space:
                return
            existing = db.scalar(select(Agent).where(Agent.factory_id == factory_id, Agent.role == "Load Balancer"))
            if existing:
                return
            agent = Agent(
                factory_id=factory_id,
                space_id=space.id,
                name="Factory Load Balancer",
                role="Load Balancer",
                objective="Reduce the factory task queue.",
                responsibilities=["triage queued tasks", "coordinate agents"],
                model=settings.provider_model,
            )
            db.add(agent)
            record_event(db, factory_id, "organization_changed", {"action": "hire", "agent": agent.name, "reason": "queued task pressure"})
            db.commit()
        finally:
            db.close()

    async def reorganize(self, factory_id: str, action: str, *, agent_id: str | None = None, target_agent_id: str | None = None, space_id: str | None = None) -> None:
        db = SessionLocal()
        try:
            factory = db.get(Factory, factory_id)
            if not factory:
                raise ServiceError("factory not found")
            agent = db.get(Agent, agent_id) if agent_id else None
            if agent and agent.factory_id != factory_id:
                raise ServiceError("agent does not belong to factory")
            if action == "hire":
                target_space = db.get(Space, space_id) if space_id else db.scalar(select(Space).where(Space.factory_id == factory_id).order_by(Space.created_at))
                if not target_space or target_space.factory_id != factory_id:
                    raise ServiceError("space does not belong to factory")
                agent = Agent(factory_id=factory_id, space_id=target_space.id, name="New Factory Agent", role="Generalist", objective=factory.primary_objective, responsibilities=[])
                db.add(agent)
                detail = {"action": "hire", "agent_id": agent.id}
            elif not agent:
                raise ServiceError("agent is required")
            elif action == "hibernate":
                reassigned = list(db.scalars(select(Task).where(Task.factory_id == factory_id, Task.assignee_id == agent.id, Task.status.in_(["queued", "running"]))))
                for task in reassigned:
                    task.assignee_id = None
                    task.status = "queued"
                    task.lease_until = None
                    task.error = "requeued because assigned agent was hibernated"
                agent.status = "hibernated"
                agent.current_task_id = None
                detail = {"action": action, "agent_id": agent.id, "requeued_tasks": [task.id for task in reassigned]}
            elif action == "move":
                target_space = db.get(Space, space_id) if space_id else None
                if not target_space or target_space.factory_id != factory_id:
                    raise ServiceError("target space does not belong to factory")
                agent.space_id = target_space.id
                detail = {"action": action, "agent_id": agent.id, "space_id": target_space.id}
            elif action == "move_responsibility":
                target = db.get(Agent, target_agent_id) if target_agent_id else None
                if not target or target.factory_id != factory_id or target.id == agent.id:
                    raise ServiceError("responsibility target does not belong to factory")
                responsibilities = list(agent.responsibilities)
                if not responsibilities:
                    raise ServiceError("agent has no responsibility to move")
                responsibility = responsibilities.pop(0)
                agent.responsibilities = responsibilities
                target.responsibilities = list(dict.fromkeys([*target.responsibilities, responsibility]))
                detail = {"action": action, "agent_id": agent.id, "target_agent_id": target.id, "responsibility": responsibility}
            elif action == "merge":
                target = db.get(Agent, target_agent_id) if target_agent_id else None
                if not target or target.factory_id != factory_id or target.id == agent.id:
                    raise ServiceError("merge target does not belong to factory")
                reassigned = list(db.scalars(select(Task).where(Task.factory_id == factory_id, Task.assignee_id == agent.id, Task.status.in_(["queued", "running"]))))
                for task in reassigned:
                    task.assignee_id = target.id
                    task.lease_until = None
                    if task.status == "running":
                        task.status = "queued"
                target.responsibilities = list(dict.fromkeys([*target.responsibilities, *agent.responsibilities]))
                target.relationships = {**target.relationships, **agent.relationships}
                agent.status = "merged"
                agent.current_task_id = None
                detail = {"action": action, "agent_id": agent.id, "target_agent_id": target.id, "reassigned_tasks": [task.id for task in reassigned]}
            else:
                raise ServiceError("unsupported organization action")
            record_event(db, factory_id, "organization_changed", detail)
            db.commit()
        finally:
            db.close()

    async def _evaluate(self, db: Session, factory: Factory, run: FactoryRun) -> None:
        goals = list(db.scalars(select(Goal).where(Goal.factory_id == factory.id, Goal.status.in_(["pending", "running"]))))
        changed = False
        for goal in goals:
            tasks = list(db.scalars(select(Task).where(Task.goal_id == goal.id)))
            artifacts = list(db.scalars(select(Artifact).where(Artifact.factory_id == factory.id, Artifact.task_id.in_([task.id for task in tasks])))) if tasks else []
            evidence_text = "\n".join(
                [task.description + " " + json.dumps(task.outputs, ensure_ascii=False) for task in tasks if task.status == "done"]
                + [artifact.content for artifact in artifacts]
            ).casefold()
            criteria = [str(item).strip() for item in goal.criteria if str(item).strip()]
            checks = []
            for criterion in criteria:
                needle = criterion.casefold()
                checks.append({"criterion": criterion, "passed": needle in evidence_text, "evidence": "matched task/artifact output" if needle in evidence_text else "no matching task/artifact evidence"})
            passed = bool(criteria) and bool(tasks) and all(check["passed"] for check in checks) and all(task.status in {"done", "cancelled"} for task in tasks)
            goal.evaluation = {"criteria": checks, "passed": passed, "task_count": len(tasks), "artifact_count": len(artifacts)}
            if passed:
                goal.status = "completed"
                goal.completion_note = "All declared criteria were evidenced by completed task outputs or artifacts."
                goal.completed_at = now()
                decision = "complete"
            else:
                goal.status = "running" if tasks else "pending"
                decision = "incomplete"
            record_event(db, factory.id, "goal_evaluated", {"goal_id": goal.id, "decision": decision, "evaluation": goal.evaluation})
            changed = True
        if changed:
            db.commit()
        open_goals = db.scalar(select(func.count(Goal.id)).where(Goal.factory_id == factory.id, Goal.status.not_in(["completed", "cancelled"]))) or 0
        queued = db.scalar(select(func.count(Task.id)).where(Task.factory_id == factory.id, Task.status.in_(["queued", "running"]))) or 0
        if open_goals == 0 and queued == 0:
            factory.status = "completed"
            run.status = "completed"
            run.stopped_at = now()
            record_event(db, factory.id, "factory_completed", {"run_id": run.id})
            db.commit()


runtime = Runtime()
