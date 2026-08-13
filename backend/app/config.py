from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.fernet import Fernet


def _poll_seconds() -> float:
    try:
        return max(0.1, float(os.getenv("RUNTIME_POLL_SECONDS", "2")))
    except ValueError:
        return 2.0


def _lease_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("TASK_LEASE_SECONDS", "60")))
    except ValueError:
        return 60.0


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("ENVIRONMENT", "development")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./factory.db")
    secret_key: str = os.getenv("SECRET_KEY", "")
    master_key: str = os.getenv("MASTER_KEY", "")
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    api_url: str = os.getenv("API_URL", "http://localhost:8000")
    provider_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    provider_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    provider_api_key: str = os.getenv("OPENAI_API_KEY", "")
    runtime_poll_seconds: float = _poll_seconds()
    task_lease_seconds: float = _lease_seconds()
    oauth_github_client_id: str = os.getenv("GITHUB_CLIENT_ID", "")
    oauth_github_client_secret: str = os.getenv("GITHUB_CLIENT_SECRET", "")
    oauth_google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    oauth_google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    oauth_github_redirect_uri: str = os.getenv("GITHUB_REDIRECT_URI", "http://localhost:8000/api/auth/oauth/callback")
    oauth_google_redirect_uri: str = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/oauth/callback")

    def validate(self) -> None:
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be set to at least 32 characters")
        if not self.master_key:
            raise RuntimeError("MASTER_KEY must be set to a Fernet key")
        try:
            Fernet(self.master_key)
        except ValueError as exc:
            raise RuntimeError("MASTER_KEY must be a valid Fernet key") from exc


settings = Settings()
