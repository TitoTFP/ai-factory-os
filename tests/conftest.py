from __future__ import annotations

import os
from pathlib import Path

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
TEST_DB = Path(__file__).resolve().parents[1] / "test-suite.db"
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
else:
    os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["MASTER_KEY"] = "X2FHjTTGfPR5ixOXRbs5Op0PTJQfokcKdMrWYEQMoK8="
os.environ["RUNTIME_POLL_SECONDS"] = "0.1"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User


@pytest.fixture(scope="session", autouse=True)
def database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    if TEST_DATABASE_URL:
        db = SessionLocal()
        db.add_all([
            User(id=user_id, email=f"{user_id}@test.invalid", name=user_id)
            for user_id in ("owner", "u", "u1", "u2")
        ])
        db.commit()
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    if not TEST_DATABASE_URL and TEST_DB.exists():
        TEST_DB.unlink()


@pytest.fixture
def client(database):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def user(client):
    response = client.post("/api/auth/register", json={"email": "owner@example.com", "name": "Owner", "password": "password123"})
    if response.status_code == 409:
        response = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "password123"})
    response.raise_for_status()
    return response.json()


@pytest.fixture
def auth(user):
    return {"Authorization": f"Bearer {user['access_token']}"}
