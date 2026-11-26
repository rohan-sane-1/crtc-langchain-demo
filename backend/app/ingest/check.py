"""Read-only "what's missing from the knowledge base?" check.

One listing request to the CRTC, no downloads, no database writes. Compare with
``pipeline.sync_case_knowledge_base``, which also mutates.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from langsmith import traceable
from sqlalchemy import select

from app.config import get_settings
from app.crtc import records
from app.ingest.pipeline import CaseNotFound, Deps, default_deps, error_code, is_retryable
from app.models import Case, Document, DocumentStatus

MISSING_CAP = 25


def _base(url: str) -> str:
    return url.split("#", 1)[0]


def _needs_retry(d: Document, now: datetime) -> bool:
    """A document the next sync would try again (and might succeed on)."""
    if d.status == DocumentStatus.pending:
        return True
    if d.status == DocumentStatus.failed:
        return is_retryable(d.error)
    if d.status == DocumentStatus.blocked:
        if error_code(d.error) == "robots_disallowed":
            return False  # robots.txt says no; re-asking changes nothing
        window = timedelta(seconds=get_settings().kb_blocked_retry_s)
        return d.attempted_at is None or now - d.attempted_at >= window
    return False


@traceable(
    name="check_case_missing_documents",
    run_type="tool",
    process_inputs=lambda i: {"case_id": str(i.get("case_id"))},
)
async def check_case_missing_documents(case_id: uuid.UUID, deps: Deps | None = None) -> dict[str, Any]:
    deps = deps or default_deps()
    now = datetime.now(UTC)
    async with deps.session_factory() as s:
        case = await s.get(Case, case_id)
        if case is None:
            raise CaseNotFound(str(case_id))
        docs = list(await s.scalars(select(Document).where(Document.case_id == case_id)))
    in_kb_embedded = sum(
        1
        for d in docs
        if d.status == DocumentStatus.embedded and d.kind != "archive"
    )

    try:
        listing = await records.list_record_documents(case, deps.client, sign_archives=False)
    except records.RecordListingError as e:
        return {
            "status": "listing_failed",
            "error": str(e)[:300],
            "in_kb_embedded": in_kb_embedded,
            "missing_count": 0,
            "missing": [],
            "removed_count": 0,
            "retryable_count": sum(1 for d in docs if _needs_retry(d, now)),
            "checked_at": now.isoformat(),
        }

    live = {d.url: d for d in listing.documents}
    known = {d.source_url for d in docs if d.kind != "archive_member"}
    missing = [d for url, d in live.items() if url not in known]
    removed = 0
    if listing.listing_ok:
        removed = sum(
            1 for d in docs if d.kind in ("filing", "archive") and _base(d.source_url) not in live
        )
    retryable = sum(1 for d in docs if _needs_retry(d, now))

    if not listing.listing_ok:
        status = "listing_failed"  # partial: related docs listed, but filings endpoint failed
    elif missing or retryable or removed:
        status = "missing_documents"
    else:
        status = "up_to_date"
    return {
        "status": status,
        "live_total": len(live),
        "in_kb_embedded": in_kb_embedded,
        "missing_count": len(missing),
        "missing": [
            {
                "title": d.title,
                "filer": d.filer,
                "filed_date": d.filed_date.isoformat() if d.filed_date else None,
                "url": d.url,
            }
            for d in missing[:MISSING_CAP]
        ],
        "removed_count": removed,
        "retryable_count": retryable,
        "source_errors": listing.source_errors,
        "checked_at": now.isoformat(),
    }
