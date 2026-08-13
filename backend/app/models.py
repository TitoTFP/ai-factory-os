from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid4())


def json_list() -> list[Any]:
    return []


def json_dict() -> dict[str, Any]:
    return {}


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OAuthState(Base):
    __tablename__ = "oauth_states"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(512))
    email: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Factory(Base):
    __tablename__ = "factories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    mission: Mapped[str] = mapped_column(Text)
    primary_objective: Mapped[str] = mapped_column(Text)
    constraints: Mapped[list[Any]] = mapped_column(JSON, default=json_list)
    autonomy: Mapped[str] = mapped_column(String(32), default="mostly_autonomous")
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class FactoryCredential(Base):
    __tablename__ = "factory_credentials"
    __table_args__ = (UniqueConstraint("factory_id", "provider", name="uq_factory_credential_provider"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(1024))
    model: Mapped[str] = mapped_column(String(160))
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    permissions: Mapped[list[Any]] = mapped_column(JSON, default=json_list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Space(Base):
    __tablename__ = "spaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="active")
    shared_memory: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(160))
    objective: Mapped[str] = mapped_column(Text, default="")
    responsibilities: Mapped[list[Any]] = mapped_column(JSON, default=json_list)
    model: Mapped[str] = mapped_column(String(160), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="idle", index=True)
    current_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    relationships: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    private_memory: Mapped[list[Any]] = mapped_column(JSON, default=json_list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    objective: Mapped[str] = mapped_column(Text)
    criteria: Mapped[list[Any]] = mapped_column(JSON, default=json_list)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    completion_note: Mapped[str] = mapped_column(Text, default="")
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("factory_id", "idempotency_key", name="uq_factory_message_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    sender_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    recipient_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    message_type: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(240), default="")
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    space_id: Mapped[str | None] = mapped_column(ForeignKey("spaces.id", ondelete="SET NULL"), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(240))
    kind: Mapped[str] = mapped_column(String(64), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    uri: Mapped[str] = mapped_column(String(1024), default="")
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=json_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Tool(Base):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("factory_id", "name", name="uq_factory_tool_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    permissions: Mapped[list[Any]] = mapped_column(JSON, default=json_list)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    actor_type: Mapped[str] = mapped_column(String(32), default="system")
    actor_id: Mapped[str] = mapped_column(String(36), default="system")
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=json_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class FactoryRun(Base):
    __tablename__ = "factory_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="stopped", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Usage(Base):
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    factory_id: Mapped[str] = mapped_column(ForeignKey("factories.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="openai-compatible")
    model: Mapped[str] = mapped_column(String(160), default="")
    request_kind: Mapped[str] = mapped_column(String(64), default="agent")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
