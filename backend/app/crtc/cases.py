"""CRTC proceedings list: fetch, parse/validate, upsert, cache, search."""

import asyncio
import json
import logging
import re
import weakref
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crtc.client import CrtcClient, CrtcClientError
from app.crtc.html import Link, extract_links, html_to_text
from app.models import Case

log = logging.getLogger(__name__)

MAX_RAW_FIELD_LEN = 200_000  # per raw JSON field; larger => the record is dropped
UPSERT_BATCH = 500  # 13 columns per row; keeps well under asyncpg's 32k bind-parameter limit
MAX_QUERY_WORDS = 8
MAX_PAGE_SIZE = 100
MAX_LIMIT = MAX_PAGE_SIZE  # kept for callers of the Phase 2 API

StatusFilter = Literal["open", "closed", "all"]
SectorFilter = Literal["broadcasting", "telecom", "compliance"]
SortKey = Literal["deadline", "posted_desc", "posted_asc", "notice_asc", "notice_desc", "relevance"]
STATUS_VALUES: tuple[str, ...] = ("open", "closed", "all")
SORT_VALUES: tuple[str, ...] = (
    "deadline",
    "posted_desc",
    "posted_asc",
    "notice_asc",
    "notice_desc",
    "relevance",
)
# Filter value -> substring of the stored sector. The upstream field also holds combinations
# such as "Broadcasting and Telecom", which should match both filters.
SECTOR_LABELS: dict[str, str] = {
    "broadcasting": "Broadcasting",
    "telecom": "Telecom",
    "compliance": "Compliance and Enforcement",
}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        1,
    )
}
# "Deadline for submission of interventions: 20 November 2026" (also "Revised deadline ...",
# "... of interventions/comments/answers: 17 December 2026"). Plain-text dates, not markup.
_SUBJECT_DEADLINE_RE = re.compile(
    r"deadline for submission of interventions[^:|]{0,40}:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)
_FILE_NO_RE = re.compile(r"^\d{4}-[A-Z]+\d*-\d{6,9}$")


class CrtcParseError(Exception):
    """The payload as a whole is unusable (not the `{"data": [...]}` shape, or nothing valid)."""


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        raise ValueError("boolean where text expected")  # noqa: TRY004
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    raise ValueError(f"unexpected type {type(v).__name__}")


RawStr = Annotated[
    str, BeforeValidator(_coerce_str), Field(default="", max_length=MAX_RAW_FIELD_LEN)
]


class RawRecord(BaseModel):
    """One element of `data`, English fields only. Unknown fields (the `...Fr` twins, `CC`,
    `keyword`, ...) are ignored. Missing/None fields become ''."""

    model_config = ConfigDict(extra="ignore")

    EventNo: RawStr
    Subject: RawStr
    PostedDt: RawStr
    IntrDt: RawStr
    Doc: RawStr
    PT: RawStr
    ET: RawStr
    Status: RawStr
    Applct: RawStr
    Prov: RawStr
    City: RawStr
    Search: RawStr


class CaseRecord(BaseModel):
    """Clean, validated proceeding. All text is plain text (no markup), length-capped."""

    notice_number: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=2000)
    sector: str | None = Field(default=None, max_length=100)
    proceeding_type: str | None = Field(default=None, max_length=100)
    status: Literal["open", "closed", "other"]
    posted_date: date
    intervention_deadline: date | None = None
    deadline_source: Literal["intrdt", "subject"] | None = None
    applicant: str | None = Field(default=None, max_length=200)
    province: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=2048)
    notice_links: list[Link] = Field(default_factory=list, max_length=50)
    document_links: list[Link] = Field(default_factory=list, max_length=50)


@dataclass
class ParseResult:
    records: list[CaseRecord] = field(default_factory=list)
    total_raw: int = 0
    dropped_invalid: int = 0  # failed validation
    dropped_duplicate: int = 0  # notice_number already seen in this payload (first one wins)
    rejected_links: int = 0  # hrefs discarded (javascript:, off-host, malformed, ...)
    upgraded_links: int = 0  # http:// links on CRTC hosts recovered by upgrading to https://

    @property
    def dropped(self) -> int:
        return self.dropped_invalid + self.dropped_duplicate

    @property
    def open_without_deadline(self) -> int:
        return sum(r.status == "open" and r.intervention_deadline is None for r in self.records)

    @property
    def deadline_from_subject(self) -> int:
        return sum(r.deadline_source == "subject" for r in self.records)


def _normalize_status(raw: str) -> str:
    # Upstream also uses "All"/"ALL" for record types with no open/closed lifecycle
    # (agreement applications, final-offer arbitrations).
    s = raw.strip().lower()
    return s if s in ("open", "closed") else "other"


def _first_date(html: str) -> date | None:
    text = html_to_text(html, max_len=500)
    m = _DATE_RE.search(text)
    if not m:
        return None  # e.g. "N/A"
    try:
        return date.fromisoformat(m.group())
    except ValueError:
        return None


def _subject_deadline(subject_text: str) -> date | None:
    """Last 'deadline for submission of interventions: D Month YYYY' in the Subject text
    (the last one is the revised deadline when the notice was amended)."""
    found: date | None = None
    for d, month, y in _SUBJECT_DEADLINE_RE.findall(subject_text):
        m = _MONTHS.get(month.lower())
        if m:
            try:
                found = date(int(y), m, int(d))
            except ValueError:
                continue
    return found


def _derive_applicant(proceeding_type: str, applct: str, lines: list[str]) -> str | None:
    """Applicant name. Only the licence-renewal records carry it in `Applct`; for the other
    record types it sits at a fixed position in the Subject lines. Where the layout isn't
    recognised we return None rather than guess (notices of consultation have several parties)."""
    if applct:
        return applct
    if not lines:
        return None
    first_is_file_no = bool(_FILE_NO_RE.match(lines[0]))
    n = len(lines)
    if proceeding_type == "Part 1 Applications":
        if first_is_file_no:
            return lines[-1] if n == 3 else None  # telecom: file no | description | applicant
        return lines[0] if n >= 3 else None  # broadcasting: applicant | description | ...
    if proceeding_type.startswith("Basic International Telecommunications Services"):
        return lines[1] if first_is_file_no and n >= 2 else None
    if proceeding_type == "Agreement Applications":
        return lines[-1] if first_is_file_no and n == 3 else None
    if proceeding_type == "Tariff Applications":
        return lines[-1] if first_is_file_no and n >= 4 else None
    if proceeding_type in ("Administrative - Ownership", "Final Offer Arbitration"):
        return lines[0] if not first_is_file_no else None
    return None


def _opt(text: str) -> str | None:
    return text or None


def to_case(raw: RawRecord, base_url: str) -> tuple[CaseRecord, int, int]:
    """Convert a raw record into a CaseRecord. Returns (record, rejected_links, upgraded_links).
    Raises pydantic.ValidationError if the result is invalid."""
    notice_links, rej1, up1 = extract_links(raw.EventNo, base_url)
    doc_links, rej2, up2 = extract_links(raw.Doc, base_url)
    notice = html_to_text(raw.Search, max_len=200).strip()
    proceeding_type = html_to_text(raw.ET, max_len=100)
    subject_lines = html_to_text(raw.Subject, max_len=20_000, sep="\n").split("\n")
    applicant = _derive_applicant(
        proceeding_type, html_to_text(raw.Applct, max_len=200), subject_lines
    )

    # IntrDt holds the CURRENT deadline (it shows "New deadline" after amendments); the Subject
    # text only has the dates as originally announced, so it's only a fallback.
    deadline, source = _first_date(raw.IntrDt), "intrdt"
    if deadline is None:
        deadline = _subject_deadline(html_to_text(raw.Subject, max_len=20_000, sep=" "))
        source = "subject"
    rec = CaseRecord(
        notice_number=notice,
        title=html_to_text(raw.Subject, max_len=2000),
        sector=_opt(html_to_text(raw.PT, max_len=100)),
        proceeding_type=_opt(proceeding_type),
        status=_normalize_status(raw.Status),  # type: ignore[arg-type]
        posted_date=raw.PostedDt.strip(),  # type: ignore[arg-type]  # pydantic parses ISO date
        intervention_deadline=deadline,
        deadline_source=source if deadline else None,  # type: ignore[arg-type]
        applicant=_opt((applicant or "")[:200]),
        province=_opt(html_to_text(raw.Prov, max_len=100)),
        city=_opt(html_to_text(raw.City, max_len=200)),
        source_url=notice_links[0].url if notice_links else None,
        notice_links=notice_links,
        document_links=doc_links,
    )
    return rec, rej1 + rej2, up1 + up2


def parse_cases(payload: bytes | str, base_url: str) -> ParseResult:
    """Parse the list endpoint's body. Bad records are dropped and counted, never fatal.
    Raises CrtcParseError only if the payload as a whole is unusable."""
    try:
        doc = json.loads(payload)
    except (ValueError, RecursionError) as e:
        raise CrtcParseError("response is not valid JSON") from e
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        raise CrtcParseError("response has no top-level 'data' list")

    result = ParseResult(total_raw=len(data))
    seen: set[str] = set()
    for item in data:
        try:
            if not isinstance(item, dict):
                raise TypeError("record is not an object")
            rec, rejected, upgraded = to_case(RawRecord.model_validate(item), base_url)
        except (ValidationError, ValueError, TypeError):
            result.dropped_invalid += 1
            continue
        result.rejected_links += rejected
        result.upgraded_links += upgraded
        if rec.notice_number in seen:
            result.dropped_duplicate += 1
            continue
        seen.add(rec.notice_number)
        result.records.append(rec)

    if data and not result.records:
        raise CrtcParseError(f"all {len(data)} records were invalid")
    log.info(
        "parsed CRTC list: raw=%d valid=%d dropped_invalid=%d dropped_duplicate=%d "
        "rejected_links=%d upgraded_links=%d open_without_deadline=%d",
        result.total_raw,
        len(result.records),
        result.dropped_invalid,
        result.dropped_duplicate,
        result.rejected_links,
        result.upgraded_links,
        result.open_without_deadline,
    )
    return result


async def fetch_case_list(client: CrtcClient, url: str | None = None) -> ParseResult:
    url = url or get_settings().crtc_list_url
    resp = await client.fetch(url)
    # JSON + HTML parsing of ~18 MB is CPU-bound; keep it off the event loop.
    return await asyncio.to_thread(parse_cases, resp.content, resp.url)


# --------------------------------------------------------------------------- persistence


async def upsert_cases(session: AsyncSession, records: list[CaseRecord], now: datetime) -> int:
    """Idempotent upsert keyed on notice_number, all in ONE transaction (rolled back on error).
    `last_synced_at` / `record_fingerprint` (owned by Phase 3) are never touched."""
    if not records:
        return 0
    rows = [
        {
            "notice_number": r.notice_number,
            "title": r.title,
            "sector": r.sector,
            "proceeding_type": r.proceeding_type,
            "status": r.status,
            "posted_date": datetime.combine(r.posted_date, datetime.min.time(), tzinfo=UTC),
            "posted_year": r.posted_date.year,
            "source_url": r.source_url,
            "applicant": r.applicant,
            "province": r.province,
            "intervention_deadline": r.intervention_deadline,
            "document_links": [{"url": link.url, "text": link.text} for link in r.document_links],
            "last_listed_at": now,
        }
        for r in records
    ]
    async with session.begin():
        for i in range(0, len(rows), UPSERT_BATCH):
            stmt = pg_insert(Case).values(rows[i : i + UPSERT_BATCH])
            stmt = stmt.on_conflict_do_update(
                index_elements=[Case.notice_number],
                set_={c: stmt.excluded[c] for c in rows[0] if c != "notice_number"},
            )
            await session.execute(stmt)
    return len(rows)


@dataclass
class RefreshResult:
    fetched: bool  # False => served from cache (within TTL)
    stale: bool = False  # True => a refresh was due but failed; DB data is older than the TTL
    error: str | None = None
    total_raw: int | None = None
    upserted: int | None = None
    dropped_records: int | None = None
    dropped_invalid: int | None = None
    dropped_duplicate: int | None = None
    rejected_links: int | None = None
    upgraded_links: int | None = None
    open_without_deadline: int | None = None
    last_listed_at: datetime | None = None


_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)
_last_refresh: RefreshResult | None = None
_default_client: CrtcClient | None = None


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


def get_default_client() -> CrtcClient:
    global _default_client
    if _default_client is None:
        _default_client = CrtcClient()
    return _default_client


async def _last_listed(session: AsyncSession) -> datetime | None:
    return await session.scalar(select(func.max(Case.last_listed_at)))


async def refresh_cases(
    session: AsyncSession,
    client: CrtcClient | None = None,
    *,
    ttl: timedelta | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> RefreshResult:
    """Re-fetch the list at most once per TTL. The DB (`max(cases.last_listed_at)`) is the
    cache, so it survives restarts. Concurrent callers share one fetch. If a due refresh
    fails, existing data is served and the result is flagged `stale`."""
    ttl = ttl or timedelta(minutes=get_settings().crtc_list_ttl_minutes)
    now = now or datetime.now(UTC)
    async with _lock():
        last = await _last_listed(session)
        await session.rollback()  # end the implicit read transaction; upsert opens its own
        if not force and last is not None and now - last < ttl:
            return RefreshResult(fetched=False, last_listed_at=last)
        try:
            parsed = await fetch_case_list(client or get_default_client())
            upserted = await upsert_cases(session, parsed.records, now)
        except (CrtcClientError, CrtcParseError) as e:
            log.error("CRTC list refresh failed: %s", e)
            return RefreshResult(fetched=False, stale=True, error=str(e), last_listed_at=last)
        global _last_refresh
        _last_refresh = RefreshResult(
            fetched=True,
            total_raw=parsed.total_raw,
            upserted=upserted,
            dropped_records=parsed.dropped,
            dropped_invalid=parsed.dropped_invalid,
            dropped_duplicate=parsed.dropped_duplicate,
            rejected_links=parsed.rejected_links,
            upgraded_links=parsed.upgraded_links,
            open_without_deadline=parsed.open_without_deadline,
            last_listed_at=now,
        )
        return _last_refresh


# --------------------------------------------------------------------------- searching


class InvalidFilter(ValueError):
    """A filter value is outside its allowlist / typed schema (the route maps this to 422)."""


@dataclass(frozen=True)
class CaseFilters:
    q: str | None = None
    status: str = "open"
    sector: str | None = None
    proceeding_type: str | None = None
    year: int | None = None
    applicant: str | None = None
    province: str | None = None
    closing_within: int | None = None  # 7 or 30 days from today
    deadline_from: date | None = None
    deadline_to: date | None = None
    has_documents: bool = False


def _words(text: str | None) -> list[str]:
    return (text or "").strip()[:200].split()[:MAX_QUERY_WORDS]


def validate_static(f: CaseFilters, sort: str) -> None:
    """Checks that need no DB. Raises InvalidFilter."""
    if f.status not in STATUS_VALUES:
        raise InvalidFilter(f"status must be one of {STATUS_VALUES}")
    if f.sector is not None and f.sector not in SECTOR_LABELS:
        raise InvalidFilter(f"sector must be one of {tuple(SECTOR_LABELS)}")
    if sort not in SORT_VALUES:
        raise InvalidFilter(f"sort must be one of {SORT_VALUES}")
    if f.closing_within not in (None, 7, 30):
        raise InvalidFilter("closing_within must be 7 or 30")
    if f.year is not None and not 1990 <= f.year <= 2100:
        raise InvalidFilter("year out of range")
    if f.deadline_from and f.deadline_to and f.deadline_from > f.deadline_to:
        raise InvalidFilter("deadline_from must not be after deadline_to")
    for name in ("q", "applicant"):
        if len(getattr(f, name) or "") > 200:
            raise InvalidFilter(f"{name} too long")


async def validate_values(session: AsyncSession, f: CaseFilters) -> None:
    """proceeding_type and province are allowlisted against the values actually in the data."""
    for name, col in (("proceeding_type", Case.proceeding_type), ("province", Case.province)):
        value = getattr(f, name)
        if value is None:
            continue
        exists = await session.scalar(select(func.count()).select_from(Case).where(col == value))
        if not exists:
            raise InvalidFilter(f"unknown {name}")


def build_conditions(f: CaseFilters, today: date, exclude: frozenset[str] = frozenset()) -> list:
    """SQLAlchemy expressions only (every value becomes a bind parameter). `exclude` drops
    one filter group, used to compute facet counts."""
    c: list = []
    if "status" not in exclude and f.status != "all":
        c.append(Case.status == f.status)
    if "sector" not in exclude and f.sector:
        c.append(Case.sector.contains(SECTOR_LABELS[f.sector], autoescape=True))
    if "type" not in exclude and f.proceeding_type:
        c.append(Case.proceeding_type == f.proceeding_type)
    if "year" not in exclude and f.year:
        c.append(Case.posted_year == f.year)
    if "province" not in exclude and f.province:
        c.append(Case.province == f.province)
    if "applicant" not in exclude and f.applicant:
        for w in _words(f.applicant):
            c.append(Case.applicant.icontains(w, autoescape=True))
    if "closing" not in exclude and f.closing_within:
        c.append(
            Case.intervention_deadline.between(today, today + timedelta(days=f.closing_within))
        )
    if "range" not in exclude:
        if f.deadline_from:
            c.append(Case.intervention_deadline >= f.deadline_from)
        if f.deadline_to:
            c.append(Case.intervention_deadline <= f.deadline_to)
    if "docs" not in exclude and f.has_documents:
        c.append(Case.has_documents.is_(True))
    if "q" not in exclude:
        for w in _words(f.q):
            c.append(
                Case.notice_number.icontains(w, autoescape=True)
                | Case.title.icontains(w, autoescape=True)
                | Case.applicant.icontains(w, autoescape=True)
            )
    return c


def order_clauses(sort: str, f: CaseFilters, today: date) -> list:
    tiebreak = [Case.posted_date.desc().nulls_last(), Case.notice_number]
    if sort == "relevance" and (f.q or "").strip():
        q = (f.q or "").strip().lower()
        notice = func.lower(Case.notice_number)
        tier = case(
            (notice == q, 0),
            (notice.startswith(q, autoescape=True), 1),
            (notice.contains(q, autoescape=True), 2),
            else_=3,
        )
        similarity = func.similarity(func.concat_ws(" ", Case.title, Case.applicant), q)
        return [tier, similarity.desc(), *tiebreak]
    if sort in ("deadline", "relevance"):
        # Upcoming deadlines first (nearest first), then past-deadline cases (most recent
        # first), then cases with no deadline (newest posted first).
        group = case(
            (Case.intervention_deadline >= today, 0),
            (Case.intervention_deadline < today, 1),
            else_=2,
        )
        distance = func.abs(cast(Case.intervention_deadline - today, Integer))
        return [group, distance, *tiebreak]
    if sort == "posted_asc":
        return [Case.posted_date.asc().nulls_last(), Case.notice_number]
    if sort == "posted_desc":
        return tiebreak
    if sort == "notice_desc":
        return [Case.notice_number.desc()]
    return [Case.notice_number.asc()]


async def _group_counts(session: AsyncSession, col, conds: list) -> list[tuple[Any, int]]:
    rows = await session.execute(
        select(col, func.count()).where(*conds, col.is_not(None)).group_by(col)
    )
    return [(v, n) for v, n in rows.all()]


async def compute_facets(session: AsyncSession, f: CaseFilters, today: date) -> dict[str, Any]:
    """Counts per option, each computed with every OTHER filter applied (standard faceting)."""

    def conds(*excl: str) -> list:
        return build_conditions(f, today, frozenset(excl))

    status = dict(await _group_counts(session, Case.status, conds("status")))
    sector_row = (
        await session.execute(
            select(
                *[
                    func.count().filter(Case.sector.contains(label, autoescape=True))
                    for label in SECTOR_LABELS.values()
                ]
            ).where(*conds("sector"))
        )
    ).one()
    types = await _group_counts(session, Case.proceeding_type, conds("type"))
    years = await _group_counts(session, Case.posted_year, conds("year"))
    provinces = await _group_counts(session, Case.province, conds("province"))
    closing_counts = {}
    for n in (7, 30):
        closing_counts[str(n)] = await session.scalar(
            select(func.count())
            .select_from(Case)
            .where(
                *conds("closing", "range"),
                Case.intervention_deadline.between(today, today + timedelta(days=n)),
            )
        )
    with_docs = await session.scalar(
        select(func.count()).select_from(Case).where(*conds("docs"), Case.has_documents.is_(True))
    )
    return {
        "status": {
            "open": status.get("open", 0),
            "closed": status.get("closed", 0),
            "all": sum(status.values()),
        },
        "sector": dict(zip(SECTOR_LABELS, sector_row, strict=True)),
        "proceeding_types": [
            {"value": v, "count": n} for v, n in sorted(types, key=lambda t: (-t[1], t[0]))
        ],
        "years": [{"value": v, "count": n} for v, n in sorted(years, reverse=True)],
        "provinces": [
            {"value": v, "count": n} for v, n in sorted(provinces, key=lambda t: (-t[1], t[0]))
        ],
        "closing": closing_counts,
        "with_documents": with_docs or 0,
    }


@dataclass
class SearchResult:
    total_cases_listed: int  # all cases in the cache, before filters (for coverage reporting)
    matched: int
    page: int
    page_size: int
    pages: int
    sort: str
    cases: list[Case]
    facets: dict[str, Any] | None = None


async def search_cases(
    session: AsyncSession,
    filters: CaseFilters,
    *,
    sort: str | None = None,
    page: int = 1,
    page_size: int = 25,
    with_facets: bool = False,
    today: date | None = None,
) -> SearchResult:
    """Filter, rank, sort and paginate cached cases. Raises InvalidFilter on bad values."""
    today = today or datetime.now(UTC).date()
    sort = sort or ("relevance" if (filters.q or "").strip() else "deadline")
    validate_static(filters, sort)
    await validate_values(session, filters)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))

    conds = build_conditions(filters, today)
    total = await session.scalar(select(func.count()).select_from(Case)) or 0
    matched = await session.scalar(select(func.count()).select_from(Case).where(*conds)) or 0
    rows = await session.scalars(
        select(Case)
        .where(*conds)
        .order_by(*order_clauses(sort, filters, today))
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    return SearchResult(
        total_cases_listed=total,
        matched=matched,
        page=page,
        page_size=page_size,
        pages=max(1, -(-matched // page_size)),
        sort=sort,
        cases=list(rows),
        facets=await compute_facets(session, filters, today) if with_facets else None,
    )


async def suggest_applicants(session: AsyncSession, q: str, limit: int = 10) -> list[dict]:
    """Type-ahead for the applicant filter. LIKE wildcards in `q` are escaped."""
    words = _words(q)
    if not words:
        return []
    stmt = (
        select(Case.applicant, func.count().label("n"))
        .where(
            Case.applicant.is_not(None),
            *[Case.applicant.icontains(w, autoescape=True) for w in words],
        )
        .group_by(Case.applicant)
        .order_by(func.count().desc(), Case.applicant)
        .limit(max(1, min(limit, 20)))
    )
    return [{"value": v, "count": n} for v, n in (await session.execute(stmt)).all()]


def case_to_dict(c: Case) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "notice_number": c.notice_number,
        "title": c.title,
        "status": c.status,
        "sector": c.sector,
        "proceeding_type": c.proceeding_type,
        "posted_date": c.posted_date.date().isoformat() if c.posted_date else None,
        "intervention_deadline": (
            c.intervention_deadline.isoformat() if c.intervention_deadline else None
        ),
        "applicant": c.applicant,
        "province": c.province,
        "document_count": len(c.document_links or []),
        "source_url": c.source_url,
    }


async def list_crtc_cases(
    session: AsyncSession,
    query: str | None = None,
    status: StatusFilter = "open",
    sector: SectorFilter | None = None,
    limit: int = 25,
    *,
    client: CrtcClient | None = None,
) -> dict[str, Any]:
    """Refresh the cache if stale, then filter. Returns a compact, JSON-friendly dict.

    Plain async function for now; Phase 4 wraps it with LangChain's @tool (the wrapper will
    inject the session) once langchain is added and its docs/version have been checked.
    """
    refresh = await refresh_cases(session, client)
    res = await search_cases(
        session, CaseFilters(q=query, status=status, sector=sector), page_size=limit
    )
    # Dropped-record stats come from the last real refresh in this process (None if none yet).
    info = refresh if refresh.fetched else _last_refresh
    return {
        "total_cases_listed": res.total_cases_listed,
        "matched": res.matched,
        "returned": len(res.cases),
        "cases": [case_to_dict(c) for c in res.cases],
        "refreshed": refresh.fetched,
        "stale": refresh.stale,
        "refresh_error": refresh.error,
        "last_listed_at": refresh.last_listed_at.isoformat() if refresh.last_listed_at else None,
        "dropped_records": info.dropped_records if info else None,
    }
