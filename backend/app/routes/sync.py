"""Knowledge-base sync endpoints.

POST /cases/{id}/sync        start a sync in the background (login + CSRF origin check + rate limit)
POST /cases/{id}/upload      upload a ZIP/file the user downloaded from the CRTC site by hand
GET  /cases/{id}/sync-status progress of a running sync, the last result and live coverage

CSRF: the app-wide ``check_origin`` dependency (main.py) rejects any non-GET request whose
Origin is not our frontend, in addition to the SameSite=Lax session cookie.
"""

import asyncio
import math
import uuid
from collections.abc import Callable
from typing import Any

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.config import get_settings
from app.db import get_session
from app.ingest import runner
from app.ingest.check import check_case_missing_documents
from app.ingest.pipeline import Deps, default_deps
from app.ingest.status import coverage, progress_registry
from app.models import Case, User
from app.ratelimit import SlidingWindowLimiter

router = APIRouter(prefix="/cases", tags=["sync"])

_s = get_settings()
sync_limiter = SlidingWindowLimiter(_s.kb_sync_rate_limit, _s.kb_sync_rate_window_s)
deps_factory: Callable[[], Deps] = default_deps  # tests replace this with fakes


def background_tasks() -> set[asyncio.Task]:
    return runner.background_tasks()


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
    if not runner.start_background_sync(case_id, lambda: deps_factory()):
        return {"state": progress_registry.get(case_id).phase, "started": False}
    return {"state": "queued", "started": True}


@router.post("/{case_id}/upload", status_code=http_status.HTTP_202_ACCEPTED)
async def upload_documents(
    case_id: uuid.UUID,
    file: UploadFile = File(...),
    _user: User = Depends(rate_limited),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The user downloaded the proceeding's ZIP (or a single file) from the CRTC site in their
    own browser and uploads it here. Streamed to a temp file under the size cap, then ingested
    in the background; progress and the per-file result come from GET /sync-status."""
    await _case_or_404(session, case_id)
    if progress_registry.running(case_id) or runner.is_running(case_id):
        raise HTTPException(http_status.HTTP_409_CONFLICT, "A sync or upload is already running for this case")
    limit = get_settings().kb_max_upload_bytes
    name = (file.filename or "upload").replace("\\", "/").rsplit("/", 1)[-1][:200] or "upload"
    fd = tempfile.NamedTemporaryFile(prefix="crtc-upload-", delete=False)
    path = Path(fd.name)
    try:
        size = 0
        with fd:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"The file is larger than the {limit // (1024 * 1024)} MB upload limit",
                    )
                fd.write(chunk)
        if size == 0:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "The file is empty")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if not runner.start_background_upload(case_id, name, path, lambda: deps_factory()):
        path.unlink(missing_ok=True)
        raise HTTPException(http_status.HTTP_409_CONFLICT, "A sync or upload is already running for this case")
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


@router.get("/{case_id}/kb-check")
async def kb_check(
    case_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Read-only: one listing request to the CRTC, compared with what is in the knowledge base."""
    await _case_or_404(session, case_id)
    return await check_case_missing_documents(case_id, deps_factory())
