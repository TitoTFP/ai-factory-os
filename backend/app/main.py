from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .deps import current_user, owned_factory
from .models import Agent, Artifact, Event, Factory, FactoryCredential, FactoryRun, Goal, Message, OAuthState, Space, Task, Usage, User, now
from .schemas import (
    AgentResponse,
    ArchitectResponse,
    ArtifactResponse,
    CredentialUpdate,
    EventResponse,
    FactoryCreate,
    FactoryResponse,
    FactorySnapshot,
    GoalResponse,
    LoginRequest,
    MessageResponse,
    OAuthCallback,
    RunResponse,
    UsageResponse,
    SpaceResponse,
    TaskResponse,
    TokenResponse,
    UserCreate,
    UserResponse,
    TaskCreate,
    MessageCreate,
    OAuthStart,
    OrganizationChange,
)
from .oauth import OAuthError, verify_oauth_code
from .security import create_token, decode_token, encrypt_secret, hash_password, new_oauth_state, utc_now, verify_password
from .network import validate_external_url
from .services import architect_factory, credential_for, record_event, runtime


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.environment == "production":
        settings.validate()
    Base.metadata.create_all(bind=engine)
    await runtime.start()
    yield
    await runtime.shutdown()


app = FastAPI(title="AI Factory OS", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def token_for(user: User) -> TokenResponse:
    return TokenResponse(access_token=create_token(user.id), user=UserResponse.model_validate(user))


def _factory_response(factory: Factory) -> FactoryResponse:
    return FactoryResponse.model_validate(factory)


def _factory_query(model: Any, factory_id: str):
    return select(model).where(model.factory_id == factory_id)


def _usage_response(db: Session, factory: Factory) -> UsageResponse:
    row = db.execute(
        select(
            func.coalesce(func.sum(Usage.prompt_tokens), 0),
            func.coalesce(func.sum(Usage.completion_tokens), 0),
            func.coalesce(func.sum(Usage.total_tokens), 0),
            func.coalesce(func.sum(Usage.cost_usd), 0.0),
            func.count(Usage.id),
        ).where(Usage.factory_id == factory.id)
    ).one()
    credential = credential_for(db, factory.id)
    return UsageResponse(
        factory_id=factory.id,
        provider=credential.provider if credential else "openai-compatible",
        model=credential.model if credential else "",
        prompt_tokens=int(row[0] or 0),
        completion_tokens=int(row[1] or 0),
        total_tokens=int(row[2] or 0),
        cost_usd=float(row[3] or 0.0),
        requests=int(row[4] or 0),
    )


def _snapshot(db: Session, factory: Factory) -> FactorySnapshot:
    where = lambda model: _factory_query(model, factory.id)  # noqa: E731
    run = db.scalar(select(FactoryRun).where(FactoryRun.factory_id == factory.id).order_by(FactoryRun.created_at.desc()))
    usage = _usage_response(db, factory)
    return FactorySnapshot(
        factory=_factory_response(factory),
        spaces=[SpaceResponse.model_validate(x) for x in db.scalars(where(Space))],
        agents=[AgentResponse.model_validate(x) for x in db.scalars(where(Agent))],
        goals=[GoalResponse.model_validate(x) for x in db.scalars(where(Goal))],
        tasks=[TaskResponse.model_validate(x) for x in db.scalars(where(Task))],
        messages=[MessageResponse.model_validate(x) for x in db.scalars(where(Message).order_by(Message.created_at.desc()).limit(100))],
        artifacts=[ArtifactResponse.model_validate(x) for x in db.scalars(where(Artifact).order_by(Artifact.created_at.desc()).limit(100))],
        events=[EventResponse.model_validate(x) for x in db.scalars(where(Event).order_by(Event.created_at.desc()).limit(200))],
        run=RunResponse.model_validate(run) if run else None,
        usage=usage,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/auth/register", response_model=TokenResponse, status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> TokenResponse:
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="email already registered")
    user = User(email=payload.email, name=payload.name.strip(), password_hash=hash_password(payload.password.get_secret_value()))
    db.add(user)
    db.commit()
    db.refresh(user)
    return token_for(user)


@app.post("/api/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if not user or not verify_password(payload.password.get_secret_value(), user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    return token_for(user)


@app.post("/api/auth/oauth/start")
def oauth_start(payload: OAuthStart, db: Session = Depends(get_db)) -> dict[str, str]:
    client_id = settings.oauth_github_client_id if payload.provider == "github" else settings.oauth_google_client_id
    if not client_id:
        raise HTTPException(status_code=503, detail=f"{payload.provider} OAuth is not configured")
    state = new_oauth_state()
    db.add(OAuthState(id=state, provider=payload.provider, expires_at=utc_now() + timedelta(minutes=10)))
    db.commit()
    redirect_uri = settings.oauth_github_redirect_uri if payload.provider == "github" else settings.oauth_google_redirect_uri
    if payload.provider == "github":
        url = f"https://github.com/login/oauth/authorize?client_id={quote(client_id)}&redirect_uri={quote(redirect_uri)}&scope=read:user%20user:email&state={quote(state)}"
    else:
        url = f"https://accounts.google.com/o/oauth2/v2/auth?client_id={quote(client_id)}&redirect_uri={quote(redirect_uri)}&response_type=code&scope=openid%20email%20profile&state={quote(state)}"
    return {"authorization_url": url, "state": state}


async def _finish_oauth(provider: Literal["github", "google"], code: str, state_id: str, db: Session) -> TokenResponse:
    state = db.scalar(select(OAuthState).where(OAuthState.id == state_id, OAuthState.provider == provider))
    if not state or state.used_at or state.expires_at < utc_now():
        raise HTTPException(status_code=400, detail="invalid or expired OAuth state")
    redirect_uri = settings.oauth_github_redirect_uri if provider == "github" else settings.oauth_google_redirect_uri
    try:
        identity_data = await verify_oauth_code(provider, code, redirect_uri, settings)
    except OAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    state.used_at = utc_now()
    identity = db.scalar(select(models.OAuthIdentity).where(models.OAuthIdentity.provider == provider, models.OAuthIdentity.subject == identity_data["subject"]))
    if identity:
        user = db.get(User, identity.user_id)
    else:
        user = db.scalar(select(User).where(User.email == identity_data["email"]))
        if not user:
            user = User(email=identity_data["email"], name=identity_data["name"])
            db.add(user)
            db.flush()
        db.add(models.OAuthIdentity(user_id=user.id, provider=provider, subject=identity_data["subject"], email=identity_data["email"]))
    if user is None:
        raise HTTPException(status_code=409, detail="OAuth identity is not attached to a user")
    db.commit()
    db.refresh(user)
    return token_for(user)


@app.post("/api/auth/oauth/callback", response_model=TokenResponse)
async def oauth_callback(payload: OAuthCallback, db: Session = Depends(get_db)) -> TokenResponse:
    return await _finish_oauth(payload.provider, payload.code, payload.state, db)


@app.get("/api/auth/oauth/callback")
async def oauth_callback_redirect(
    provider: Literal["github", "google"],
    code: str,
    state: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    token = await _finish_oauth(provider, code, state, db)
    return RedirectResponse(f"{settings.frontend_url}/oauth/callback#access_token={quote(token.access_token)}")


@app.get("/api/me", response_model=UserResponse)
def me(user: User = Depends(current_user)) -> UserResponse:
    return UserResponse.model_validate(user)


@app.get("/api/factories", response_model=list[FactoryResponse])
def list_factories(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[FactoryResponse]:
    return [_factory_response(x) for x in db.scalars(select(Factory).where(Factory.owner_id == user.id).order_by(Factory.created_at.desc()))]


@app.post("/api/factories", response_model=FactoryResponse, status_code=201)
def create_factory(payload: FactoryCreate, db: Session = Depends(get_db), user: User = Depends(current_user)) -> FactoryResponse:
    factory = Factory(
        owner_id=user.id,
        name=payload.name.strip(),
        mission=payload.mission.strip(),
        primary_objective=payload.primary_objective.strip(),
        constraints=payload.constraints,
        autonomy=payload.autonomy,
        status="draft",
    )
    db.add(factory)
    db.flush()
    base_url = payload.provider_base_url or settings.provider_base_url
    try:
        validate_external_url(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        encrypted_api_key = encrypt_secret(payload.provider_api_key.get_secret_value())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    provider_model = payload.provider_model or settings.provider_model
    db.add(
        FactoryCredential(
            factory_id=factory.id,
            provider="openai-compatible",
            base_url=base_url,
            model=provider_model,
            encrypted_api_key=encrypted_api_key,
            permissions=payload.tool_permissions,
        )
    )
    record_event(
        db,
        factory.id,
        "factory_created",
        {"name": factory.name, "provider": "openai-compatible", "base_url": base_url, "model": provider_model, "permissions": sorted(set(payload.tool_permissions))},
        actor_type="user",
        actor_id=user.id,
    )
    db.commit()
    db.refresh(factory)
    return _factory_response(factory)


@app.get("/api/factories/{factory_id}", response_model=FactorySnapshot)
def get_factory(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> FactorySnapshot:
    return _snapshot(db, owned_factory(factory_id, db, user))


@app.post("/api/factories/{factory_id}/architect", response_model=ArchitectResponse)
async def run_architect(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> ArchitectResponse:
    factory = owned_factory(factory_id, db, user)
    try:
        result = await architect_factory(db, factory)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.refresh(factory)
    return ArchitectResponse(
        factory=_factory_response(factory),
        spaces=[SpaceResponse.model_validate(x) for x in result["spaces"]],
        agents=[AgentResponse.model_validate(x) for x in result["agents"]],
        goals=[GoalResponse.model_validate(x) for x in result["goals"]],
    )


@app.put("/api/factories/{factory_id}/credentials", status_code=204)
def update_credentials(factory_id: str, payload: CredentialUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)) -> None:
    if payload.provider != "openai-compatible":
        raise HTTPException(status_code=422, detail="only openai-compatible is supported in v0.1")
    factory = owned_factory(factory_id, db, user)
    credential = credential_for(db, factory.id)
    if not credential:
        credential = FactoryCredential(factory_id=factory.id, provider=payload.provider)
        db.add(credential)
    try:
        validate_external_url(payload.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    credential.base_url = payload.base_url
    credential.model = payload.model
    try:
        credential.encrypted_api_key = encrypt_secret(payload.api_key.get_secret_value())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    credential.permissions = payload.permissions
    record_event(
        db,
        factory.id,
        "credentials_updated",
        {"provider": payload.provider, "permissions": sorted(set(payload.permissions)), "base_url": payload.base_url, "model": payload.model},
        actor_type="user",
        actor_id=user.id,
    )
    db.commit()


@app.post("/api/factories/{factory_id}/resume", response_model=RunResponse)
def resume_run(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> RunResponse:
    return start_run(factory_id, db, user)


@app.post("/api/factories/{factory_id}/run", response_model=RunResponse)
def start_run(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> RunResponse:
    factory = owned_factory(factory_id, db, user)
    run = db.scalar(select(FactoryRun).where(FactoryRun.factory_id == factory.id, FactoryRun.status.in_(["running", "paused", "stopped"])).order_by(FactoryRun.created_at.desc()))
    if not run:
        run = FactoryRun(factory_id=factory.id, status="running", started_at=now())
        db.add(run)
        db.flush()
    else:
        run.status = "running"
        run.started_at = run.started_at or now()
    factory.status = "running"
    record_event(db, factory.id, "factory_started", {"run_id": run.id}, actor_type="user", actor_id=user.id)
    db.commit()
    db.refresh(run)
    return RunResponse.model_validate(run)


@app.post("/api/factories/{factory_id}/pause", response_model=RunResponse)
def pause_run(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> RunResponse:
    factory = owned_factory(factory_id, db, user)
    run = db.scalar(select(FactoryRun).where(FactoryRun.factory_id == factory.id, FactoryRun.status == "running").order_by(FactoryRun.created_at.desc()))
    if not run:
        raise HTTPException(status_code=409, detail="factory is not running")
    run.status = "paused"
    factory.status = "paused"
    record_event(db, factory.id, "factory_paused", {"run_id": run.id}, actor_type="user", actor_id=user.id)
    db.commit()
    return RunResponse.model_validate(run)


@app.post("/api/factories/{factory_id}/stop", response_model=RunResponse)
def stop_run(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> RunResponse:
    factory = owned_factory(factory_id, db, user)
    run = db.scalar(select(FactoryRun).where(FactoryRun.factory_id == factory.id, FactoryRun.status.in_(["running", "paused"])).order_by(FactoryRun.created_at.desc()))
    if not run:
        raise HTTPException(status_code=409, detail="factory has no active run")
    run.status = "stopped"
    run.stopped_at = now()
    factory.status = "stopped"
    record_event(db, factory.id, "factory_stopped", {"run_id": run.id}, actor_type="user", actor_id=user.id)
    db.commit()
    return RunResponse.model_validate(run)


@app.post("/api/factories/{factory_id}/organization", status_code=204)
async def change_organization(factory_id: str, payload: OrganizationChange, db: Session = Depends(get_db), user: User = Depends(current_user)) -> None:
    factory = owned_factory(factory_id, db, user)
    try:
        await runtime.reorganize(factory.id, payload.action, agent_id=payload.agent_id, target_agent_id=payload.target_agent_id, space_id=payload.space_id)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/factories/{factory_id}/tasks", response_model=TaskResponse, status_code=201)
def create_task(factory_id: str, payload: TaskCreate, db: Session = Depends(get_db), user: User = Depends(current_user)) -> TaskResponse:
    factory = owned_factory(factory_id, db, user)
    if payload.assignee_id and not db.scalar(select(Agent).where(Agent.id == payload.assignee_id, Agent.factory_id == factory.id)):
        raise HTTPException(status_code=404, detail="assignee not found")
    if payload.goal_id and not db.scalar(select(Goal).where(Goal.id == payload.goal_id, Goal.factory_id == factory.id)):
        raise HTTPException(status_code=404, detail="goal not found")
    if payload.parent_id and not db.scalar(select(Task).where(Task.id == payload.parent_id, Task.factory_id == factory.id)):
        raise HTTPException(status_code=404, detail="parent task not found")
    task = Task(factory_id=factory.id, title=payload.title, description=payload.description, goal_id=payload.goal_id, parent_id=payload.parent_id, assignee_id=payload.assignee_id, inputs=payload.inputs, max_retries=payload.max_retries, created_by=user.id)
    db.add(task)
    db.flush()
    record_event(db, factory.id, "task_created", {"task_id": task.id, "title": task.title}, actor_type="user", actor_id=user.id)
    db.commit()
    db.refresh(task)
    return TaskResponse.model_validate(task)


@app.get("/api/factories/{factory_id}/usage", response_model=UsageResponse)
def get_usage(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> UsageResponse:
    factory = owned_factory(factory_id, db, user)
    return _usage_response(db, factory)


@app.post("/api/factories/{factory_id}/messages", response_model=MessageResponse, status_code=201)
def create_message(factory_id: str, payload: MessageCreate, db: Session = Depends(get_db), user: User = Depends(current_user)) -> MessageResponse:
    factory = owned_factory(factory_id, db, user)
    if payload.sender_agent_id and not db.scalar(select(Agent).where(Agent.id == payload.sender_agent_id, Agent.factory_id == factory.id)):
        raise HTTPException(status_code=404, detail="sender not found")
    if payload.recipient_agent_id and not db.scalar(select(Agent).where(Agent.id == payload.recipient_agent_id, Agent.factory_id == factory.id)):
        raise HTTPException(status_code=404, detail="recipient not found")
    if payload.idempotency_key:
        existing = db.scalar(select(Message).where(Message.factory_id == factory.id, Message.idempotency_key == payload.idempotency_key))
        if existing:
            return MessageResponse.model_validate(existing)
    message = Message(factory_id=factory.id, sender_agent_id=payload.sender_agent_id, recipient_agent_id=payload.recipient_agent_id, message_type=payload.message_type, subject=payload.subject, body=payload.body, payload=payload.payload, correlation_id=payload.correlation_id, idempotency_key=payload.idempotency_key, status="delivered" if payload.recipient_agent_id else "queued", delivered_at=now() if payload.recipient_agent_id else None)
    try:
        # The unique constraint is the authority under concurrent requests;
        # the pre-read above is only the fast path.
        with db.begin_nested():
            db.add(message)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(Message).where(Message.factory_id == factory.id, Message.idempotency_key == payload.idempotency_key)) if payload.idempotency_key else None
        if existing:
            return MessageResponse.model_validate(existing)
        raise HTTPException(status_code=409, detail="message idempotency key already exists")
    record_event(db, factory.id, "message_published", {"message_id": message.id, "type": message.message_type}, actor_type="user", actor_id=user.id)
    db.commit()
    db.refresh(message)
    return MessageResponse.model_validate(message)


@app.get("/api/factories/{factory_id}/messages", response_model=list[MessageResponse])
def list_messages(factory_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[MessageResponse]:
    factory = owned_factory(factory_id, db, user)
    return [MessageResponse.model_validate(item) for item in db.scalars(select(Message).where(Message.factory_id == factory.id).order_by(Message.created_at.desc()).limit(200))]


@app.post("/api/factories/{factory_id}/messages/{message_id}/read", response_model=MessageResponse)
def mark_message_read(factory_id: str, message_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> MessageResponse:
    factory = owned_factory(factory_id, db, user)
    message = db.scalar(select(Message).where(Message.id == message_id, Message.factory_id == factory.id))
    if not message:
        raise HTTPException(status_code=404, detail="message not found")
    message.status = "read"
    message.read_at = now()
    db.commit()
    db.refresh(message)
    return MessageResponse.model_validate(message)


@app.post("/api/factories/{factory_id}/goals/{goal_id}/complete", response_model=GoalResponse)
def complete_goal(factory_id: str, goal_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> GoalResponse:
    factory = owned_factory(factory_id, db, user)
    goal = db.scalar(select(Goal).where(Goal.id == goal_id, Goal.factory_id == factory.id))
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")
    goal.status = "completed"
    goal.completion_note = "Completed by user override."
    goal.completed_at = now()
    record_event(db, factory.id, "goal_completed_by_user", {"goal_id": goal.id}, actor_type="user", actor_id=user.id)
    db.commit()
    return GoalResponse.model_validate(goal)


@app.websocket("/api/factories/{factory_id}/events")
async def event_stream(websocket: WebSocket, factory_id: str) -> None:
    token = websocket.query_params.get("token", "")
    payload = decode_token(token)
    user_id = payload.get("sub") if payload else None
    db = SessionLocal()
    try:
        factory = db.scalar(select(Factory).where(Factory.id == factory_id, Factory.owner_id == user_id)) if user_id else None
    finally:
        db.close()
    if not factory:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    last_created_at = None
    last_event_id = ""
    try:
        while True:
            db = SessionLocal()
            try:
                if last_created_at is None:
                    events = list(db.scalars(select(Event).where(Event.factory_id == factory_id).order_by(Event.created_at.desc(), Event.id.desc()).limit(100)))
                    events.reverse()
                else:
                    events = list(db.scalars(select(Event).where(
                        Event.factory_id == factory_id,
                        or_(Event.created_at > last_created_at, and_(Event.created_at == last_created_at, Event.id > last_event_id)),
                    ).order_by(Event.created_at.asc(), Event.id.asc()).limit(100)))
                for event in events:
                    await websocket.send_json(EventResponse.model_validate(event).model_dump(mode="json"))
                    last_created_at = event.created_at
                    last_event_id = event.id
            finally:
                db.close()
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError):
        return


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
