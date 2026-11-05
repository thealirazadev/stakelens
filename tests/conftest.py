import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import Base, make_session_factory
from app.main import create_app

SERVER_URL = os.environ.get(
    "TEST_DATABASE_SERVER", "postgresql+psycopg://stakelens:stakelens@localhost:55432"
)
TEST_DATABASE_URL = f"{SERVER_URL}/stakelens_test"


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    admin = create_engine(f"{SERVER_URL}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text("DROP DATABASE IF EXISTS stakelens_test WITH (FORCE)"))
        connection.execute(text("CREATE DATABASE stakelens_test"))
    admin.dispose()

    # Build the schema through the migration so the tests also cover it.
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
    command.upgrade(config, "head")

    test_engine = create_engine(TEST_DATABASE_URL)
    yield test_engine
    test_engine.dispose()


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY"))
    yield make_session_factory(engine)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL, confirmations=3)


@pytest.fixture
def client(engine: Engine, sessions, settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings, engine=engine))
