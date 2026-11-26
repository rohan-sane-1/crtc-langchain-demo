"""Scheduled knowledge-base refresh ("cron"): keep cases people are chatting about current.

Sequential on purpose: the CRTC client already limits itself to ~1 request/second, and a
nightly/6-hourly job has no reason to hurry.
"""

import logging
import uuid
from typing import Any

from langsmith import traceable
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.check import check_case_missing_documents
from app.ingest.pipeline import Deps, default_deps, sync_case_knowledge_base
from app.models import Case, Chat

log = logging.getLogger(__name__)


async def _case_ids(scope: str) -> list[uuid.UUID]:
    q = select(Case.id).where(Case.has_documents)
    if scope == "chatted":
        q = q.where(Case.id.in_(select(Chat.case_id).where(Chat.case_id.is_not(None))))
    elif scope != "all":
        raise ValueError(f"unknown scope {scope!r} (use 'chatted' or 'all')")
    async with SessionLocal() as s:
        return list(await s.scalars(q))


@traceable(name="scheduled_kb_refresh", run_type="chain")
async def refresh_all_cases(scope: str | None = None, deps: Deps | None = None) -> dict[str, Any]:
    scope = scope or get_settings().refresh_case_scope
    deps = deps or default_deps()
    summary: dict[str, Any] = {
        "scope": scope, "cases": 0, "up_to_date": 0, "refreshed": 0, "errors": 0, "details": [],
    }
    for case_id in await _case_ids(scope):
        summary["cases"] += 1
        try:
            check = await check_case_missing_documents(case_id, deps)
            if check["status"] != "up_to_date" and (
                check.get("missing_count") or check.get("retryable_count") or check.get("removed_count")
            ):
                res = await sync_case_knowledge_base(case_id, deps=deps, force=True)
                summary["refreshed"] += 1
                summary["details"].append(
                    {"case_id": str(case_id), "missing": check.get("missing_count"),
                     "newly_embedded": res.get("newly_embedded"), "status": res.get("status")}
                )
            else:
                summary["up_to_date"] += 1
        except Exception as e:
            summary["errors"] += 1
            log.exception("refresh of one case failed (%s)", type(e).__name__)
    log.info(
        "scheduled refresh: %d cases, %d up to date, %d refreshed, %d errors",
        summary["cases"], summary["up_to_date"], summary["refreshed"], summary["errors"],
    )
    return summary
