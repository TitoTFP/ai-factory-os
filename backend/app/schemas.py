from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    email: str
    password: SecretStr
    name: str = ""

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or len(value) > 320:
            raise ValueError("valid email is required")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 8:
            raise ValueError("password must contain at least 8 characters")
        return value


class LoginRequest(BaseModel):
    email: str
    password: SecretStr


class OAuthStart(BaseModel):
    provider: Literal["github", "google"]


class OAuthCallback(BaseModel):
    provider: Literal["github", "google"]
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserResponse"


class UserResponse(APIModel):
    id: str
    email: str
    name: str


class FactoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    mission: str = Field(min_length=1)
    primary_objective: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    autonomy: Literal["mostly_autonomous", "fully_autonomous", "supervised"] = "mostly_autonomous"
    provider_base_url: str | None = None
    provider_model: str | None = None
    provider_api_key: SecretStr
    tool_permissions: list[str] = Field(default_factory=lambda: ["workspace", "web_fetch", "http"])


class FactoryResponse(APIModel):
    id: str
    name: str
    mission: str
    primary_objective: str
    constraints: list[Any]
    autonomy: str
    status: str


class CredentialUpdate(BaseModel):
    provider: str = "openai-compatible"
    base_url: str
    model: str
    api_key: SecretStr
    permissions: list[str] = Field(default_factory=list)


class SpaceResponse(APIModel):
    id: str
    factory_id: str
    name: str
    purpose: str
    status: str


class AgentResponse(APIModel):
    id: str
    factory_id: str
    space_id: str
    name: str
    role: str
    objective: str
    responsibilities: list[Any]
    model: str
    status: str
    current_task_id: str | None


class GoalResponse(APIModel):
    id: str
    factory_id: str
    title: str
    objective: str
    criteria: list[Any]
    status: str
    completion_note: str
    evaluation: dict[str, Any]


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    goal_id: str | None = None
    parent_id: str | None = None
    assignee_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=2, ge=0, le=10)


class MessageCreate(BaseModel):
    message_type: Literal["MESSAGE", "TASK_REQUEST", "TASK_RESULT", "QUESTION", "REVIEW_REQUEST", "REVIEW_RESULT", "ARTIFACT", "DECISION", "ESCALATION", "BROADCAST"]
    body: str = Field(min_length=1)
    subject: str = ""
    sender_agent_id: str | None = None
    recipient_agent_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(APIModel):
    id: str
    factory_id: str
    goal_id: str | None
    parent_id: str | None
    assignee_id: str | None
    title: str
    description: str
    status: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    retry_count: int
    error: str


class MessageResponse(APIModel):
    id: str
    factory_id: str
    sender_agent_id: str | None
    recipient_agent_id: str | None
    message_type: str
    subject: str
    body: str
    payload: dict[str, Any]
    correlation_id: str | None
    idempotency_key: str | None
    status: str
    delivered_at: Any | None
    read_at: Any | None


class ArtifactResponse(APIModel):
    id: str
    factory_id: str
    space_id: str | None
    agent_id: str | None
    task_id: str | None
    name: str
    kind: str
    content: str
    uri: str
    extra: dict[str, Any]


class EventResponse(APIModel):
    id: str
    factory_id: str
    actor_type: str
    actor_id: str
    event_type: str
    payload: dict[str, Any]


class RunResponse(APIModel):
    id: str
    factory_id: str
    status: str
    last_error: str


class UsageResponse(APIModel):
    factory_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    requests: int


class OrganizationChange(BaseModel):
    action: Literal["hire", "hibernate", "merge", "move", "move_responsibility"]
    agent_id: str | None = None
    target_agent_id: str | None = None
    space_id: str | None = None


class ArchitectResponse(BaseModel):
    factory: FactoryResponse
    spaces: list[SpaceResponse]
    agents: list[AgentResponse]
    goals: list[GoalResponse]


class FactorySnapshot(BaseModel):
    factory: FactoryResponse
    spaces: list[SpaceResponse]
    agents: list[AgentResponse]
    goals: list[GoalResponse]
    tasks: list[TaskResponse]
    messages: list[MessageResponse]
    artifacts: list[ArtifactResponse]
    events: list[EventResponse]
    run: RunResponse | None
    usage: UsageResponse
