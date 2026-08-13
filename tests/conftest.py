from __future__ import annotations

import os
from pathlib import Path

TEST_DB = Path(__file__).resolve().parents[1] / "test-suite.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["MASTER_KEY"] = "X2FHjTTGfPR5ixOXRbs5Op0PTJQfokcKdMrWYEQMoK8="
os.environ["RUNTIME_POLL_SECONDS"] = "0.1"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    if TEST_DB.exists():
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
