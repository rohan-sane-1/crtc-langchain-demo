import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.crtc.cases import (
    MAX_PAGE_SIZE,
    CaseFilters,
    InvalidFilter,
    SectorFilter,
    SortKey,
    StatusFilter,
    case_to_dict,
    refresh_cases,
    search_cases,
    suggest_applicants,
)
from app.db import get_session
from app.models import Case, User

router = APIRouter(prefix="/cases", tags=["cases"])


class CaseOut(BaseModel):
    id: uuid.UUID
    notice_number: str
    title: str | None
    status: str | None
    sector: str | None
    proceeding_type: str | None
    posted_date: str | None
    intervention_deadline: str | None
    applicant: str | None
    province: str | None
    document_count: int
    source_url: str | None


class CasesOut(BaseModel):
    total_cases_listed: int
    matched: int
    returned: int
    page: int
    page_size: int
    pages: int
    sort: str
    cases: list[CaseOut]
    facets: dict[str, Any] | None
    refreshed: bool
    stale: bool
    refresh_error: str | None
    last_listed_at: datetime | None
    dropped_records: int | None


class ApplicantOut(BaseModel):
    value: str
    count: int


@router.get("", response_model=CasesOut)
async def get_cases(
    # Literal types make FastAPI reject anything outside the allowlist with a 422.
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: StatusFilter = "open",
    sector: SectorFilter | None = None,
    proceeding_type: Annotated[str | None, Query(max_length=100)] = None,
    year: Annotated[int | None, Query(ge=1990, le=2100)] = None,
    applicant: Annotated[str | None, Query(max_length=200)] = None,
    province: Annotated[str | None, Query(max_length=100)] = None,
    closing_within: Annotated[int | None, Query(description="7 or 30")] = None,
    deadline_from: date | None = None,
    deadline_to: date | None = None,
    has_documents: bool = False,
    sort: SortKey | None = None,
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    filters = CaseFilters(
        q=q,
        status=status,
        sector=sector,
        proceeding_type=proceeding_type,
        year=year,
        applicant=applicant,
        province=province,
        closing_within=closing_within,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        has_documents=has_documents,
    )
    refresh = await refresh_cases(session)  # no-op while the cache is fresh
    try:
        res = await search_cases(
            session, filters, sort=sort, page=page, page_size=page_size, with_facets=True
        )
    except InvalidFilter as e:
        raise HTTPException(422, str(e)) from e
    if res.total_cases_listed == 0 and refresh.stale:
        # Nothing cached and the CRTC fetch failed: don't pretend the list is empty.
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, "CRTC list unavailable")
    from app.crtc import cases as cases_mod  # last real refresh stats, if any in this process

    info = refresh if refresh.fetched else cases_mod._last_refresh
    return {
        "total_cases_listed": res.total_cases_listed,
        "matched": res.matched,
        "returned": len(res.cases),
        "page": res.page,
        "page_size": res.page_size,
        "pages": res.pages,
        "sort": res.sort,
        "cases": [case_to_dict(c) for c in res.cases],
        "facets": res.facets,
        "refreshed": refresh.fetched,
        "stale": refresh.stale,
        "refresh_error": refresh.error,
        "last_listed_at": refresh.last_listed_at,
        "dropped_records": info.dropped_records if info else None,
    }


@router.get("/applicants", response_model=list[ApplicantOut])
async def applicant_suggestions(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await suggest_applicants(session, q)


@router.get("/{case_id}", response_model=CaseOut)
async def get_case(
    case_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    case = await session.get(Case, case_id)
    if case is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Case not found")
    return case_to_dict(case)
