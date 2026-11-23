"""Filters, sorting, ranking, pagination, facets and input validation for case search.
Uses the throwaway Postgres from conftest (skipped if docker compose isn't up)."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.crtc import cases as cases_mod
from app.crtc.cases import (
    CaseFilters,
    CaseRecord,
    InvalidFilter,
    search_cases,
    suggest_applicants,
    upsert_cases,
)
from app.crtc.html import Link
from app.main import app
from app.models import User

NOW = datetime.now(UTC)
TODAY = NOW.date()


def d(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def mk(notice, **kw) -> CaseRecord:
    docs = kw.pop("docs", 0)
    base = {
        "notice_number": notice,
        "title": f"Title of {notice}",
        "status": "open",
        "sector": "Telecom",
        "proceeding_type": "Part 1 Applications",
        "posted_date": date(2026, 1, 1),
        "document_links": [Link(f"https://crtc.gc.ca/{notice}/{i}", "doc") for i in range(docs)],
    }
    base.update(kw)
    return CaseRecord(**base)


DATASET = [
    mk(
        "2026-259",
        title="Reducing red tape - telecommunications reporting requirements",
        proceeding_type="Notice of Consultation",
        posted_date=date(2026, 10, 6),
        intervention_deadline=d(10),
        docs=2,
    ),
    mk(
        "2026-259-1",
        title="Amendment to procedure",
        proceeding_type="Notice of Consultation",
        posted_date=date(2026, 10, 20),
        intervention_deadline=d(3),
    ),
    mk(
        "2025-259",
        title="Unrelated item that mentions 2026-259 in its text",
        posted_date=date(2025, 3, 3),
        intervention_deadline=d(1),
    ),
    mk(
        "R-100",
        title="Licence renewal",
        sector="Broadcasting",
        proceeding_type="Part 1 Licence Renewal Applications",
        applicant="Bell Media Inc.",
        province="Ontario",
        posted_date=date(2025, 5, 5),
        intervention_deadline=d(40),
        docs=1,
    ),
    mk("PAST-1", title="Past deadline but still open", intervention_deadline=d(-5)),
    mk("NODL-OLD", title="No deadline older", posted_date=date(2026, 9, 1)),
    mk("NODL-NEW", title="No deadline newer", posted_date=date(2026, 11, 1)),
    mk(
        "C-2020",
        title="Compliance matter",
        status="closed",
        sector="Compliance and Enforcement",
        proceeding_type="Part 1 Licence Renewal Applications",
        applicant="Rogers Communications Canada Inc.",
        province="Quebec",
        posted_date=date(2020, 6, 6),
        intervention_deadline=d(-100),
        docs=3,
    ),
    mk(
        "X-ALL",
        title="Agreement with no lifecycle",
        status="other",
        sector="Broadcasting and Telecom",
        proceeding_type="Agreement Applications",
        applicant="Bell Canada",
        posted_date=date(2024, 4, 4),
    ),
    mk("PCT-1", title="Fully 100% sure_thing wildcard test", posted_date=date(2023, 3, 3)),
]


@pytest.fixture
async def s(db_session):
    await upsert_cases(db_session, DATASET, NOW)
    return db_session


async def run(session, **kw):
    sort = kw.pop("sort", None)
    page = kw.pop("page", 1)
    page_size = kw.pop("page_size", 25)
    res = await search_cases(
        session,
        CaseFilters(**kw),
        sort=sort,
        page=page,
        page_size=page_size,
        with_facets=True,
        today=TODAY,
    )
    return res


def notices(res) -> list[str]:
    return [c.notice_number for c in res.cases]


# ----------------------------------------------------------------------------- default view


async def test_default_view_is_open_sorted_by_nearest_upcoming_deadline(s):
    res = await run(s)
    assert res.sort == "deadline"
    assert notices(res) == [
        "2025-259",  # +1 day
        "2026-259-1",  # +3
        "2026-259",  # +10
        "R-100",  # +40
        "PAST-1",  # deadline passed: after every upcoming one
        "NODL-NEW",  # no deadline last, newest posted first
        "NODL-OLD",
        "PCT-1",
    ]
    assert res.matched == 8 and res.total_cases_listed == 10


# ----------------------------------------------------------------------------- each filter


async def test_status_filter(s):
    assert {c.status for c in (await run(s, status="open")).cases} == {"open"}
    assert notices(await run(s, status="closed")) == ["C-2020"]
    assert (await run(s, status="all")).matched == 10  # includes the "other" lifecycle record


async def test_sector_filter_including_combined_sector_values(s):
    tel = await run(s, status="all", sector="telecom")
    assert "X-ALL" in notices(tel) and "R-100" not in notices(tel)  # combo matches both
    bc = await run(s, status="all", sector="broadcasting")
    assert set(notices(bc)) == {"R-100", "X-ALL"}
    assert notices(await run(s, status="all", sector="compliance")) == ["C-2020"]


async def test_proceeding_type_filter(s):
    res = await run(s, status="all", proceeding_type="Notice of Consultation")
    assert set(notices(res)) == {"2026-259", "2026-259-1"}


async def test_year_posted_filter(s):
    assert notices(await run(s, status="all", year=2020)) == ["C-2020"]
    assert set(notices(await run(s, status="all", year=2025))) == {"2025-259", "R-100"}
    assert (await run(s, status="all", year=1999)).matched == 0


async def test_applicant_filter_is_case_insensitive_substring(s):
    assert notices(await run(s, status="all", applicant="bell")) == ["R-100", "X-ALL"] or set(
        notices(await run(s, status="all", applicant="bell"))
    ) == {"R-100", "X-ALL"}
    assert notices(await run(s, status="all", applicant="rogers communications")) == ["C-2020"]
    assert (await run(s, status="all", applicant="nobody")).matched == 0


async def test_province_filter(s):
    assert notices(await run(s, status="all", province="Ontario")) == ["R-100"]
    assert notices(await run(s, status="all", province="Quebec")) == ["C-2020"]


async def test_closing_within_quick_filters(s):
    assert set(notices(await run(s, closing_within=7))) == {"2025-259", "2026-259-1"}
    assert set(notices(await run(s, closing_within=30))) == {"2025-259", "2026-259-1", "2026-259"}
    # Past deadlines and "no deadline" never count as "closing soon".
    assert "PAST-1" not in notices(await run(s, closing_within=30))


async def test_custom_deadline_range_is_inclusive(s):
    res = await run(s, deadline_from=d(3), deadline_to=d(10))
    assert set(notices(res)) == {"2026-259-1", "2026-259"}
    assert set(notices(await run(s, deadline_from=d(11)))) == {"R-100"}
    assert set(notices(await run(s, deadline_to=d(-1)))) == {"PAST-1"}


async def test_has_documents_filter(s):
    assert set(notices(await run(s, status="all", has_documents=True))) == {
        "2026-259",
        "R-100",
        "C-2020",
    }


async def test_search_box_covers_notice_title_and_applicant(s):
    assert "2026-259" in notices(await run(s, status="all", q="2026-259"))
    assert notices(await run(s, status="all", q="red tape")) == ["2026-259"]  # title, any case
    assert set(notices(await run(s, status="all", q="BELL"))) == {"R-100", "X-ALL"}  # applicant
    assert notices(await run(s, status="all", q="rogers renewal")) == []  # words are ANDed
    assert notices(await run(s, status="all", q="rogers compliance")) == ["C-2020"]
    assert (await run(s, status="all", q="zzz-nothing")).matched == 0


async def test_combined_filters_are_anded(s):
    res = await run(
        s,
        status="open",
        sector="telecom",
        proceeding_type="Notice of Consultation",
        year=2026,
        closing_within=30,
        has_documents=True,
        q="red",
    )
    assert notices(res) == ["2026-259"]
    assert (await run(s, status="closed", sector="telecom")).matched == 0


# ----------------------------------------------------------------------------- ranking


async def test_exact_notice_number_ranks_first(s):
    res = await run(s, status="all", q="2026-259")
    # exact match, then notice-prefix match, then a mere title mention - even though the
    # title-only match has the NEAREST deadline and would win a deadline sort.
    assert notices(res) == ["2026-259", "2026-259-1", "2025-259"]
    assert res.sort == "relevance"


async def test_exact_match_is_case_insensitive_and_trimmed(s):
    assert notices(await run(s, status="all", q="  r-100 "))[0] == "R-100"


async def test_explicit_sort_overrides_relevance(s):
    res = await run(s, status="all", q="2026-259", sort="deadline")
    assert notices(res)[0] == "2025-259"


# ----------------------------------------------------------------------------- sorting


async def test_sort_by_posted_date(s):
    desc = [c.posted_date for c in (await run(s, status="all", sort="posted_desc")).cases]
    assert desc == sorted(desc, reverse=True)
    asc = [c.posted_date for c in (await run(s, status="all", sort="posted_asc")).cases]
    assert asc == sorted(asc)


async def test_sort_by_notice_number(s):
    asc = notices(await run(s, status="all", sort="notice_asc"))
    assert asc == sorted(asc)
    desc = notices(await run(s, status="all", sort="notice_desc"))
    assert desc == sorted(desc, reverse=True)


# ----------------------------------------------------------------------------- pagination


async def test_server_side_pagination(db_session):
    many = [mk(f"P-{i:03d}", posted_date=date(2026, 1, 1) + timedelta(days=i)) for i in range(120)]
    await upsert_cases(db_session, many, NOW)
    p1 = await run(db_session, sort="notice_asc", page_size=50)
    p2 = await run(db_session, sort="notice_asc", page_size=50, page=2)
    p3 = await run(db_session, sort="notice_asc", page_size=50, page=3)
    assert (p1.matched, p1.pages) == (120, 3)
    assert [len(p.cases) for p in (p1, p2, p3)] == [50, 50, 20]
    got = notices(p1) + notices(p2) + notices(p3)
    assert got == sorted(got) and len(set(got)) == 120  # no overlap, no gaps
    assert notices(await run(db_session, page=99, page_size=50)) == []  # past the end
    assert (await run(db_session, page=99)).matched == 120  # total still reported


async def test_page_size_is_capped_at_100(db_session):
    await upsert_cases(db_session, [mk(f"P-{i:03d}") for i in range(150)], NOW)
    res = await run(db_session, page_size=10_000)
    assert res.page_size == 100 and len(res.cases) == 100 and res.pages == 2
    assert (await run(db_session, page_size=-3)).page_size == 1


async def test_pagination_is_stable_with_ties(db_session):
    same = [mk(f"T-{i:03d}") for i in range(30)]  # identical deadline (none) and posted date
    await upsert_cases(db_session, same, NOW)
    seen = []
    for p in (1, 2, 3):
        seen += notices(await run(db_session, page=p, page_size=10))
    assert len(set(seen)) == 30


# ----------------------------------------------------------------------------- facets


async def test_facet_counts_apply_other_filters_but_not_their_own(s):
    f = (await run(s, status="open", sector="telecom")).facets
    # status counts ignore the status filter itself but respect sector=telecom
    assert f["status"] == {"open": 7, "closed": 0, "all": 8}
    # sector counts ignore the sector filter but respect status=open
    assert f["sector"] == {"broadcasting": 1, "telecom": 7, "compliance": 0}
    types = {t["value"]: t["count"] for t in f["proceeding_types"]}
    assert types == {"Notice of Consultation": 2, "Part 1 Applications": 5}
    assert f["with_documents"] == 1  # only 2026-259 (open+telecom+docs)


async def test_facets_closing_years_provinces_and_documents(s):
    f = (await run(s, status="all")).facets
    assert f["closing"] == {"7": 2, "30": 3}
    assert {y["value"] for y in f["years"]} == {2020, 2023, 2024, 2025, 2026}
    assert [y["value"] for y in f["years"]] == sorted(
        (y["value"] for y in f["years"]), reverse=True
    )
    assert {p["value"] for p in f["provinces"]} == {"Ontario", "Quebec"}
    assert f["with_documents"] == 3


# ----------------------------------------------------------------------------- safety


@pytest.mark.parametrize(
    "evil", ["%%", "__", "\\", "a%b", "'; DROP TABLE cases; --", "' OR '1'='1"]
)
async def test_like_wildcards_and_sql_in_search_text_are_literals(s, evil):
    res = await run(s, status="all", q=evil)
    assert res.matched == 0
    assert (await run(s, status="all", applicant=evil)).matched == 0
    assert (await run(s, status="all")).total_cases_listed == 10  # table intact


async def test_percent_in_query_only_matches_a_literal_percent(s):
    assert notices(await run(s, status="all", q="100%")) == ["PCT-1"]
    assert notices(await run(s, status="all", q="sure_thing")) == ["PCT-1"]
    assert (await run(s, status="all", q="sure_thin_")).matched == 0  # '_' isn't a wildcard


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "bogus"},
        {"status": "OPEN"},
        {"sector": "x' OR '1'='1"},
        {"closing_within": 5},
        {"year": 1800},
        {"deadline_from": date(2026, 12, 1), "deadline_to": date(2026, 1, 1)},
        {"proceeding_type": "Not a real type"},
        {"province": "Atlantis"},
        {"q": "x" * 201},
        {"applicant": "x" * 201},
    ],
)
async def test_invalid_filter_values_are_rejected(s, kw):
    with pytest.raises(InvalidFilter):
        await run(s, **kw)


async def test_invalid_sort_is_rejected(s):
    with pytest.raises(InvalidFilter):
        await run(s, sort="title; DROP TABLE cases")


async def test_applicant_type_ahead(s):
    got = await suggest_applicants(s, "bell")
    assert {g["value"] for g in got} == {"Bell Media Inc.", "Bell Canada"}
    assert await suggest_applicants(s, "%") == []
    assert await suggest_applicants(s, "   ") == []
    assert len(await suggest_applicants(s, "e", limit=1)) == 1


# ----------------------------------------------------------------------------- HTTP layer


@pytest.fixture
async def api(s, list_client, monkeypatch):
    from app.auth import current_user
    from app.db import get_session

    monkeypatch.setattr(cases_mod, "_default_client", list_client)  # must NOT be called (fresh)

    async def _session():
        async with s.maker() as sess:
            yield sess

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[current_user] = lambda: User(id=uuid.uuid4(), email="a@b.c")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.list_client = list_client  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


async def test_route_default_view_and_response_shape(api):
    r = await api.get("/cases")
    assert r.status_code == 200
    body = r.json()
    assert [c["notice_number"] for c in body["cases"]][:3] == ["2025-259", "2026-259-1", "2026-259"]
    assert body["matched"] == 8 and body["pages"] == 1 and body["sort"] == "deadline"
    assert body["facets"]["status"]["open"] == 8
    top = body["cases"][2]
    assert top["intervention_deadline"] == d(10).isoformat() and top["document_count"] == 2
    assert api.list_client.seen == []  # cache was fresh: no CRTC request


async def test_route_filters_sort_and_pagination(api):
    r = await api.get(
        "/cases",
        params={"status": "all", "sector": "broadcasting", "sort": "notice_asc", "page_size": 1},
    )
    body = r.json()
    assert body["matched"] == 2 and body["pages"] == 2 and body["returned"] == 1
    assert body["cases"][0]["notice_number"] == "R-100"
    r2 = await api.get(
        "/cases",
        params={
            "status": "all",
            "sector": "broadcasting",
            "sort": "notice_asc",
            "page_size": 1,
            "page": 2,
        },
    )
    assert r2.json()["cases"][0]["notice_number"] == "X-ALL"
    r3 = await api.get("/cases", params={"q": "2026-259", "status": "all"})
    assert r3.json()["cases"][0]["notice_number"] == "2026-259"
    r4 = await api.get("/cases", params={"closing_within": 7, "has_documents": "false"})
    assert r4.json()["matched"] == 2


@pytest.mark.parametrize(
    "params",
    [
        {"status": "bogus"},
        {"sector": "bogus"},
        {"sort": "bogus"},
        {"sort": "title; DROP TABLE cases"},
        {"closing_within": 5},
        {"closing_within": "abc"},
        {"year": 1800},
        {"year": "abc"},
        {"deadline_from": "not-a-date"},
        {"deadline_from": "2026-12-01", "deadline_to": "2026-01-01"},
        {"proceeding_type": "Nope"},
        {"province": "Atlantis"},
        {"page": 0},
        {"page": "x"},
        {"page_size": 101},
        {"page_size": 0},
        {"has_documents": "maybe"},
        {"q": "x" * 201},
        {"applicant": "x" * 201},
    ],
)
async def test_route_invalid_filter_values_return_422(api, params):
    assert (await api.get("/cases", params=params)).status_code == 422


async def test_route_search_text_sql_injection_is_harmless(api):
    r = await api.get("/cases", params={"q": "'; DROP TABLE cases; --", "status": "all"})
    assert r.status_code == 200 and r.json()["matched"] == 0
    assert (await api.get("/cases", params={"status": "all"})).json()["matched"] == 10


async def test_route_applicant_suggestions_and_single_case(api):
    r = await api.get("/cases/applicants", params={"q": "rogers"})
    assert r.json() == [{"value": "Rogers Communications Canada Inc.", "count": 1}]
    assert (await api.get("/cases/applicants")).status_code == 422  # q required
    case_id = (await api.get("/cases", params={"q": "R-100", "status": "all"})).json()["cases"][0][
        "id"
    ]
    one = await api.get(f"/cases/{case_id}")
    assert one.status_code == 200 and one.json()["applicant"] == "Bell Media Inc."
    assert (await api.get(f"/cases/{uuid.uuid4()}")).status_code == 404
    assert (await api.get("/cases/not-a-uuid")).status_code == 422


async def test_new_routes_require_login():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in ("/cases", "/cases/applicants?q=x", f"/cases/{uuid.uuid4()}"):
            assert (await c.get(path)).status_code == 401
