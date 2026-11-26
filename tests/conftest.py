import os
import pytest

# Set all env vars before any app imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://ci:ci-only@localhost:5432/kb_test")
os.environ["API_KEYS"] = "test-key-1,test-key-2"
os.environ["TEMPORAL_HOST"] = "localhost:7233"
os.environ["FIREWORKS_API_KEY"] = "test-key"
os.environ["NOTIFICATION_WEBHOOK_URL"] = ""
