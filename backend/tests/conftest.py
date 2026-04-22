"""
Shared pytest fixtures.

Environment variables are set at module level — before any app modules are
imported — so the config singleton picks up the test values instead of the
values from .env.
"""
import os
import tempfile

# Override env before any app import so Settings() picks up test values.
# load_dotenv() does not override already-set env vars, so these win.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
os.environ.setdefault("TEMP_STORAGE_DIR", tempfile.mkdtemp(prefix="vidq_test_"))
os.environ.setdefault("BASE_URL", "http://localhost:8000")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app


@pytest.fixture(scope="session")
def db_engine():
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


@pytest.fixture()
def db_session(db_engine):
    Session = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
