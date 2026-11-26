"""LangGraph Postgres checkpointer that works on any asyncio event loop.

psycopg's *async* API cannot run on Windows' default ProactorEventLoop (and uvicorn picks the
loop type depending on --reload), so ``AsyncPostgresSaver`` is fragile on Windows. This wraps
the synchronous ``PostgresSaver`` (thread-safe through a connection pool) and runs each call in
a worker thread. Throughput is irrelevant here: a handful of checkpoint writes per chat turn.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class ThreadedPostgresSaver(PostgresSaver):
    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self, config, *, filter: dict[str, Any] | None = None, before=None, limit: int | None = None
    ) -> AsyncIterator:
        items = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path: str = ""):
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str):
        return await asyncio.to_thread(self.delete_thread, thread_id)


@contextmanager
def open_checkpointer(conn_string: str):
    """Yields a ready (tables created) saver; closes the pool on exit."""
    pool = ConnectionPool(
        conn_string,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    pool.open(wait=True, timeout=15)
    try:
        saver = ThreadedPostgresSaver(pool)
        saver.setup()
        yield saver
    finally:
        pool.close()
