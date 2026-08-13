from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import (
    Agent,
    Artifact,
    Event,
    Factory,
    FactoryCredential,
    FactoryRun,
    Goal,
    Message,
    Space,
    Usage,
    Task,
    Tool,
    now,
    new_id,
)
from .provider import OpenAICompatibleProvider, ProviderConfig, ProviderError, ProviderResponse
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


def safe_workspace_path(factory_id: str, relative: str) -> Path:
    relative = relative.strip().lstrip("/")
    candidate = (WORKSPACE_ROOT / factory_id / relative).resolve()
    root = (WORKSPACE_ROOT / factory_id).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("workspace path escapes factory boundary")
    return candidate


def write_workspace_artifact(factory_id: str, name: str, content: str) -> str:
    path = safe_workspace_path(factory_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path.relative_to(WORKSPACE_ROOT / factory_id))


def validate_external_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("absolute public http(s) URL is required")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain", "metadata.google.internal"} or hostname.endswith(".local"):
        raise ValueError("private or local network URLs are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("URL host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast, ip.is_unspecified)):
            raise ValueError("private or local network URLs are not allowed")


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
        uri = f"workspace://{name.lstrip('/')}"
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
        path = safe_workspace_path(factory.id, name)
        if operation == "write":
            content = str(arguments.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            result = {"path": name, "bytes": len(content.encode())}
        else:
            result = {"path": name, "content": path.read_text(encoding="utf-8")}
    elif tool_name in {"web_fetch", "http"}:
        method = "GET" if tool_name == "web_fetch" else str(arguments.get("method", "GET")).upper()
        if method not in tool.permissions:
            raise PermissionError(f"HTTP method is not allowed for {tool_name}: {method}")
        url = str(arguments.get("url", ""))
        validate_external_url(url)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, url, json=arguments.get("body"))
        result = {"status_code": response.status_code, "url": str(response.url), "body": response.text[:200_000]}
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
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="factory-runtime")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                db = SessionLocal()
                try:
                    factory_ids = list(db.scalars(select(FactoryRun.factory_id).where(FactoryRun.status == "running")))
                finally:
                    db.close()
                for factory_id in factory_ids:
                    await self.process_factory(factory_id)
            except (OSError, RuntimeError, ProviderError):
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.runtime_poll_seconds)
            except TimeoutError:
                pass

    async def process_factory(self, factory_id: str) -> None:
        db = SessionLocal()
        try:
            factory = db.get(Factory, factory_id)
            run = db.scalar(select(FactoryRun).where(FactoryRun.factory_id == factory_id, FactoryRun.status == "running"))
            if not factory or not run:
                return
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
            else:
                await self._evaluate(db, factory, run)
        finally:
            db.close()

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
                text = await provider.chat(
                    [
                        {"role": "system", "content": agent.system_prompt if agent else "You are a factory worker."},
                        {"role": "user", "content": f"Task: {task.title}\n{task.description}"},
                    ]
                )
                _record_usage(db, factory.id, provider.last_response, agent_id=agent.id if agent else None, task_id=task.id, model=provider.config.model)
                if agent:
                    agent.budget = {**agent.budget, "used_tokens": int(agent.budget.get("used_tokens", 0)) + provider.last_response.total_tokens, "requests": int(agent.budget.get("requests", 0)) + 1}
                tool_result = {"content": text}
            filename = f"{task.id}.md"
            artifact_content = json.dumps(tool_result, ensure_ascii=False, indent=2) if not isinstance(tool_result.get("content"), str) else tool_result["content"]
            relative = write_workspace_artifact(factory.id, filename, artifact_content)
            artifact = Artifact(
                factory_id=factory.id,
                space_id=agent.space_id if agent else None,
                agent_id=agent.id if agent else None,
                task_id=task.id,
                name=filename,
                kind="text",
                content=artifact_content,
                uri=f"workspace://{relative}",
                extra={"tool": task.inputs.get("tool", "llm")},
            )
            db.add(artifact)
            db.flush()
            task.outputs = {"artifact_id": artifact.id, "uri": artifact.uri, "result": tool_result}
            task.status = "done"
            task.lease_until = None
            if agent:
                agent.status = "idle"
                agent.current_task_id = None
            message = Message(
                factory_id=factory.id,
                sender_agent_id=agent.id if agent else None,
                recipient_agent_id=None,
                message_type="TASK_RESULT",
                subject=task.title,
                body=f"Completed task and created {filename}.",
                payload={"task_id": task.id, "artifact_id": artifact.id},
            )
            db.add(message)
            db.flush()
            record_event(db, factory.id, "task_completed", {"task_id": task.id, "artifact_id": artifact.id, "message_id": message.id}, actor_type="agent" if agent else "system", actor_id=agent.id if agent else "system")
            db.commit()
            await self._maybe_reorganize(factory.id)
        except Exception as exc:
            db.rollback()
            task = db.get(Task, task.id)
            if not task:
                return
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
                agent.status = "hibernated"
                detail = {"action": action, "agent_id": agent.id}
            elif action == "move" or action == "move_responsibility":
                target_space = db.get(Space, space_id) if space_id else None
                if not target_space or target_space.factory_id != factory_id:
                    raise ServiceError("target space does not belong to factory")
                agent.space_id = target_space.id
                detail = {"action": "move", "agent_id": agent.id, "space_id": target_space.id}
            elif action == "merge":
                target = db.get(Agent, target_agent_id) if target_agent_id else None
                if not target or target.factory_id != factory_id or target.id == agent.id:
                    raise ServiceError("merge target does not belong to factory")
                target.responsibilities = list(dict.fromkeys([*target.responsibilities, *agent.responsibilities]))
                agent.status = "merged"
                detail = {"action": "merge", "agent_id": agent.id, "target_agent_id": target.id}
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
