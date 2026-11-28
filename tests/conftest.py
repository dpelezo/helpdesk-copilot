import os
import pytest

# Set all env vars before any app imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://ci:ci-only@localhost:5432/kb_test")
os.environ["API_KEYS"] = "test-key-1,test-key-2"
os.environ["TEMPORAL_HOST"] = "localhost:7233"
os.environ["NOTIFICATION_WEBHOOK_URL"] = ""


@pytest.fixture(scope="session")
def database():
    """Create the schema once, for tests that need Postgres.

    Opt in with ``pytestmark = pytest.mark.usefixtures("database")``. Tests that
    never touch the database (knowledge model, seed, mock company and systems)
    run without Postgres. The test client skips the app's lifespan, so init_db()
    never runs; a sync engine avoids event-loop clashes."""
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine(os.environ["DATABASE_URL"])
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    engine.dispose()
