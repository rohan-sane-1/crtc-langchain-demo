"""Knowledge-base sync endpoints.

POST /cases/{id}/sync        start a sync in the background (login + CSRF origin check + rate limit)
GET  /cases/{id}/sync-status progress of a running sync, the last result and live coverage

CSRF: the app-wide ``check_origin`` dependency (main.py) rejects any non-GET request whose
Origin is not our frontend, in addition to the SameSite=Lax session cookie.
"""

import asyncio
import logging
import math
import uuid
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.config import get_settings
from app.db import get_session
from app.ingest.pipeline import Deps, default_deps, sync_case_knowledge_base
from app.ingest.status import coverage, progress_registry
from app.models import Case, User
from app.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)
router = APIRouter(prefix="/cases", tags=["sync"])

_s = get_settings()
sync_limiter = SlidingWindowLimiter(_s.kb_sync_rate_limit, _s.kb_sync_rate_window_s)
deps_factory: Callable[[], Deps] = default_deps  # tests replace this with fakes
_tasks: set[asyncio.Task] = set()
_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(get_settings().kb_max_concurrent_syncs)
    return _semaphores[loop]


def background_tasks() -> set[asyncio.Task]:
    return _tasks


def rate_limited(user: User = Depends(current_user)) -> User:
    """Per-user limit on starting syncs. 429 with a Retry-After header when exceeded."""
    wait = sync_limiter.hit(str(user.id))
    if wait is not None:
        raise HTTPException(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many sync requests; try again shortly",
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    return user


async def _run_sync(case_id: uuid.UUID) -> None:
    progress = progress_registry.get(case_id)
    try:
        async with _semaphore():
            # The button means "check now": bypass the "synced moments ago" shortcut. The
            # fingerprint diff still keeps an unchanged record to a listing request.
            await sync_case_knowledge_base(case_id, deps=deps_factory(), force=True)
    except Exception as e:
        log.exception("background sync failed (%s)", type(e).__name__)
        progress.phase, progress.error = "error", type(e).__name__
        progress.finished_at = datetime.now(UTC)


async def _case_or_404(session: AsyncSession, case_id: uuid.UUID) -> Case:
    case = await session.get(Case, case_id)
    if case is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Case not found")
    return case


@router.post("/{case_id}/sync", status_code=http_status.HTTP_202_ACCEPTED)
async def start_sync(
    case_id: uuid.UUID,
    response: Response,
    _user: User = Depends(rate_limited),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _case_or_404(session, case_id)
    if progress_registry.running(case_id):
        return {"state": progress_registry.get(case_id).phase, "started": False}
    progress = progress_registry.get(case_id)
    progress.start()
    progress.phase = "queued"
    task = asyncio.create_task(_run_sync(case_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"state": "queued", "started": True}


@router.get("/{case_id}/sync-status")
async def sync_status(
    case_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _case_or_404(session, case_id)
    progress = progress_registry.peek(case_id)
    return {
        "state": progress.phase if progress else "idle",
        "progress": progress.as_dict() if progress else None,
        "last_result": progress.result if progress else None,
        "coverage": await coverage(session, case_id),
    }
