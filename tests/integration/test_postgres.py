from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, literal, select

from app.db import engine


pytestmark = pytest.mark.postgres


def test_configured_postgresql_test_database_is_reachable(database):
    configured_url = os.getenv("TEST_DATABASE_URL", "")
    if not configured_url.startswith("postgresql"):
        pytest.skip("set TEST_DATABASE_URL to a PostgreSQL test database")

    assert engine.dialect.name == "postgresql"
    with engine.connect() as connection:
        assert connection.execute(select(literal(1))).scalar_one() == 1

    tables = set(inspect(engine).get_table_names())
    assert {"factories", "tasks", "messages", "artifacts", "events", "usage_records"} <= tables
