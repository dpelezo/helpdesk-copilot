import os
import pytest

# Set all env vars before any app imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://ci:ci-only@localhost:5432/kb_test")
os.environ["API_KEYS"] = "test-key-1,test-key-2"
os.environ["TEMPORAL_HOST"] = "localhost:7233"
os.environ["FIREWORKS_API_KEY"] = "test-key"
os.environ["NOTIFICATION_WEBHOOK_URL"] = ""


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """Create the schema once. The test client skips the app's lifespan,
    so init_db() never runs; a sync engine avoids event-loop clashes."""
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine(os.environ["DATABASE_URL"])
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    engine.dispose()
