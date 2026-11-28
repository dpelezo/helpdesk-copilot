"""Where the diagnosis graph keeps its checkpoints (ADR 0004).

With ``CHECKPOINT_DB_URL`` set (a Postgres URL), checkpoints live in Postgres, so a diagnosis
waiting for approval survives a restart of the API or worker. Without it, they live in memory,
which is enough for the eval and the unit tests.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver


@asynccontextmanager
async def checkpointer(url: str | None = None):
    url = url or os.environ.get("CHECKPOINT_DB_URL")
    if not url:
        yield InMemorySaver()
        return
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()  # creates its tables once; safe to repeat
        yield saver
