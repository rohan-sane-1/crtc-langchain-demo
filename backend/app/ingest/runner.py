"""Background knowledge-base sync: one place that starts a sync task, used by both
``POST /cases/{id}/sync`` and the agent's ``refresh_knowledge_base`` tool."""

import asyncio
import logging
import uuid
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.ingest.pipeline import Deps, default_deps, ingest_upload, sync_case_knowledge_base
from app.ingest.status import progress_registry

log = logging.getLogger(__name__)

# Tests replace this with fakes (routes.sync.deps_factory still works: see start_background_sync).
deps_factory: Callable[[], Deps] = default_deps
_tasks: set[asyncio.Task] = set()
_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(get_settings().kb_max_concurrent_syncs)
    return _semaphores[loop]


def background_tasks() -> set[asyncio.Task]:
    return _tasks


def is_running(case_id: uuid.UUID) -> bool:
    p = progress_registry.peek(case_id)
    return p is not None and p.phase in ("queued", "listing", "ingesting")


async def _run(case_id: uuid.UUID, factory: Callable[[], Deps]) -> None:
    progress = progress_registry.get(case_id)
    try:
        async with semaphore():
            # "Check now": bypass the "synced moments ago" shortcut. The fingerprint diff still
            # keeps an unchanged record down to a single listing request.
            await sync_case_knowledge_base(case_id, deps=factory(), force=True)
    except Exception as e:
        log.exception("background sync failed (%s)", type(e).__name__)
        progress.phase, progress.error = "error", type(e).__name__
        progress.finished_at = datetime.now(UTC)


def start_background_sync(
    case_id: uuid.UUID, factory: Callable[[], Deps] | None = None
) -> bool:
    """Start a sync task unless one is already running for this case. True if started."""
    if is_running(case_id):
        return False
    progress = progress_registry.get(case_id)
    progress.start()
    progress.phase = "queued"
    task = asyncio.create_task(_run(case_id, factory or deps_factory))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _run_upload(case_id: uuid.UUID, filename: str, path: Path, factory: Callable[[], Deps]) -> None:
    progress = progress_registry.get(case_id)
    try:
        async with semaphore():
            data = await asyncio.to_thread(path.read_bytes)
            result = await ingest_upload(case_id, filename, data, deps=factory(), progress=progress)
        progress.phase, progress.result = "done", result
    except Exception as e:
        log.exception("background upload failed (%s)", type(e).__name__)
        progress.phase, progress.error = "error", f"{type(e).__name__}: {e}"[:200]
        progress.result = {"status": "upload_failed", "error": str(e)[:200], "rejected": []}
    finally:
        progress.finished_at = datetime.now(UTC)
        path.unlink(missing_ok=True)


def start_background_upload(
    case_id: uuid.UUID, filename: str, path: Path, factory: Callable[[], Deps] | None = None
) -> bool:
    """Ingest an uploaded file in the background unless a sync/upload is running for this case.
    True if started; the caller deletes ``path`` itself when this returns False."""
    if is_running(case_id):
        return False
    progress = progress_registry.get(case_id)
    progress.start()
    progress.phase = "queued"
    task = asyncio.create_task(_run_upload(case_id, filename, path, factory or deps_factory))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
