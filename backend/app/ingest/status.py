"""Coverage reporting and live sync progress.

Coverage is computed from the database, so it is always truthful: every document on the
record is in exactly one of embedded / pending / failed / skipped / blocked, and the lists of
non-embedded documents are returned explicitly so nobody is led to believe the knowledge base
is complete when it is not.

Counting rule: an archive that was expanded successfully (kind "archive", status "embedded")
is a container, not a document, so it is not counted; its members are. An archive that failed
or was blocked IS counted (we know there is something on the record we could not read).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Case, Chunk, Document, DocumentStatus

LIST_CAP = 200  # items per list in a response; the totals are always exact
BENIGN_SKIP_PREFIXES = ("duplicate_format",)  # skipped on purpose; does not make the KB incomplete


def split_reason(error: str | None) -> str:
    """Stable reason code of a stored error string ("code" or "code: detail")."""
    return (error or "unknown").split(":", 1)[0].strip() or "unknown"


def _counted(case_id: uuid.UUID):
    container = and_(Document.kind == "archive", Document.status == DocumentStatus.embedded)
    return and_(Document.case_id == case_id, not_(container))


def _item(d: Document) -> dict[str, Any]:
    return {
        "title": d.title,
        "filer": d.filer,
        "url": d.source_url if d.source_url.startswith("https://") else None,
        "kind": d.kind,
        "reason": d.error,
        "reason_code": split_reason(d.error),
    }


async def coverage(session: AsyncSession, case_id: uuid.UUID) -> dict[str, Any]:
    case = await session.get(Case, case_id)
    rows = (
        await session.execute(
            select(Document.status, func.count()).where(_counted(case_id)).group_by(Document.status)
        )
    ).all()
    counts = {s.value: 0 for s in DocumentStatus}
    for status, n in rows:
        counts[status.value] = n
    total = sum(counts.values())

    async def items(status: DocumentStatus) -> list[Document]:
        if not counts[status.value]:
            return []
        q = (
            select(Document)
            .where(_counted(case_id), Document.status == status)
            .order_by(Document.filed_date.desc().nulls_last(), Document.title)
            .limit(LIST_CAP)
        )
        return list(await session.scalars(q))

    listed: dict[DocumentStatus, list[dict[str, Any]]] = {}
    for st in (
        DocumentStatus.failed,
        DocumentStatus.skipped,
        DocumentStatus.blocked,
        DocumentStatus.pending,
    ):
        listed[st] = [_item(d) for d in await items(st)]
    failed, skipped, blocked, pending = (
        listed[DocumentStatus.failed],
        listed[DocumentStatus.skipped],
        listed[DocumentStatus.blocked],
        listed[DocumentStatus.pending],
    )
    reasons: dict[str, dict[str, int]] = {}
    for status in (DocumentStatus.failed, DocumentStatus.skipped, DocumentStatus.blocked):
        if not counts[status.value]:
            continue
        q = (
            select(Document.error, func.count())
            .where(_counted(case_id), Document.status == status)
            .group_by(Document.error)
        )
        by: dict[str, int] = {}
        for err, n in (await session.execute(q)).all():
            code = split_reason(err)
            by[code] = by.get(code, 0) + n
        reasons[status.value] = by
    benign = sum(
        n for code, n in reasons.get("skipped", {}).items() if code.startswith(BENIGN_SKIP_PREFIXES)
    )
    complete = (
        total > 0
        and counts["pending"] == 0
        and counts["failed"] == 0
        and counts["blocked"] == 0
        and counts["skipped"] == benign
    )
    return {
        "case_id": str(case_id),
        "notice_number": case.notice_number if case else None,
        "total_docs_on_record": total,
        "embedded": counts["embedded"],
        "pending": counts["pending"],
        "failed": failed,
        "skipped": skipped,
        "blocked": blocked,
        "pending_docs": pending,
        "counts": counts,
        "reasons": reasons,
        "truncated": {
            s: max(0, counts[s] - LIST_CAP) for s in ("failed", "skipped", "blocked", "pending")
        },
        "complete": complete,
        "last_synced_at": case.last_synced_at.isoformat() if case and case.last_synced_at else None,
        "chunks": await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.case_id == case_id)
        ),
    }


# --------------------------------------------------------------------------- progress


@dataclass
class Progress:
    """Live state of one case's sync, shown by GET /cases/{id}/sync-status."""

    phase: str = "idle"  # idle | listing | ingesting | done | error
    total: int = 0
    done: int = 0
    current: str | None = None  # title of the document being worked on (public record text)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    events: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.phase, self.total, self.done, self.current = "listing", 0, 0, None
        self.started_at, self.finished_at, self.error, self.result = datetime.now(UTC), None, None, None
        self.events = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
        }


class ProgressRegistry:
    """In-process only (single uvicorn worker). The DB coverage is the durable truth; this adds
    "what is happening right now" and the last result."""

    def __init__(self) -> None:
        self._items: dict[uuid.UUID, Progress] = {}

    def get(self, case_id: uuid.UUID) -> Progress:
        return self._items.setdefault(case_id, Progress())

    def peek(self, case_id: uuid.UUID) -> Progress | None:
        return self._items.get(case_id)

    def running(self, case_id: uuid.UUID) -> bool:
        p = self._items.get(case_id)
        return p is not None and p.phase in ("listing", "ingesting")


progress_registry = ProgressRegistry()
