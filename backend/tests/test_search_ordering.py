"""Phase 3 step 0: America/Toronto "today" and the search tie-break (own tests, own file)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.crtc import cases as cases_mod
from app.crtc.cases import CaseFilters, CaseRecord, crtc_today, search_cases, upsert_cases


def mk(notice: str, **kw) -> CaseRecord:
    base = {
        "notice_number": notice,
        "title": f"Title of {notice}",
        "status": "open",
        "sector": "Telecom",
        "proceeding_type": "Part 1 Applications",
        "posted_date": date(2026, 1, 1),
    }
    base.update(kw)
    return CaseRecord(**base)


# ----------------------------------------------------------------------------- (a) Toronto today


def test_crtc_today_uses_toronto_not_utc_in_the_evening():
    # 2026-11-24 02:00 UTC is 21:00 on the 23rd in Toronto (EST, UTC-5).
    assert crtc_today(datetime(2026, 11, 24, 2, 0, tzinfo=UTC)) == date(2026, 11, 23)
    # Just after Toronto midnight the date rolls over.
    assert crtc_today(datetime(2026, 11, 24, 5, 0, tzinfo=UTC)) == date(2026, 11, 24)
    assert crtc_today(datetime(2026, 11, 24, 4, 59, tzinfo=UTC)) == date(2026, 11, 23)


def test_crtc_today_handles_daylight_saving():
    # EDT (UTC-4): 03:59 UTC on 15 July is still the 14th in Toronto, 04:00 UTC is the 15th.
    assert crtc_today(datetime(2026, 7, 15, 3, 59, tzinfo=UTC)) == date(2026, 7, 14)
    assert crtc_today(datetime(2026, 7, 15, 4, 0, tzinfo=UTC)) == date(2026, 7, 15)


def test_crtc_today_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        crtc_today(datetime(2026, 11, 24, 2, 0))


async def test_closing_windows_follow_toronto_date_in_the_evening(db_session, monkeypatch):
    # It is 21:00 on 23 Nov in Toronto but already 24 Nov in UTC.
    monkeypatch.setattr(cases_mod, "_utcnow", lambda: datetime(2026, 11, 24, 2, 0, tzinfo=UTC))
    toronto_today = date(2026, 11, 23)
    await upsert_cases(
        db_session,
        [
            mk("DUE-TODAY", intervention_deadline=toronto_today),  # last day: still closing
            mk("IN-7", intervention_deadline=toronto_today + timedelta(days=7)),
            mk("IN-8", intervention_deadline=toronto_today + timedelta(days=8)),
            mk("IN-30", intervention_deadline=toronto_today + timedelta(days=30)),
            mk("IN-31", intervention_deadline=toronto_today + timedelta(days=31)),
            mk("YESTERDAY", intervention_deadline=toronto_today - timedelta(days=1)),
        ],
        datetime.now(UTC),
    )

    async def notices(**kw) -> set[str]:
        res = await search_cases(db_session, CaseFilters(**kw))  # no `today=`: uses the default
        return {c.notice_number for c in res.cases}

    # With a UTC "today" (24 Nov) DUE-TODAY would be missed and IN-8 would wrongly be in.
    assert await notices(closing_within=7) == {"DUE-TODAY", "IN-7"}
    assert await notices(closing_within=30) == {"DUE-TODAY", "IN-7", "IN-8", "IN-30"}
    # Default "deadline" sort: today's deadline is still upcoming (before the passed one).
    res = await search_cases(db_session, CaseFilters())
    order = [c.notice_number for c in res.cases]
    assert order.index("DUE-TODAY") < order.index("YESTERDAY")
    assert order[0] == "DUE-TODAY"


# ----------------------------------------------------------------------------- (b) tie-break


async def test_relevance_ties_break_on_most_recently_posted_first(db_session):
    # Identical title and tier, so relevance cannot separate them. The OLDER case has the
    # alphabetically smaller notice number, so a notice-number tie-break would put it first.
    await upsert_cases(
        db_session,
        [
            mk("A-OLD", title="Wireless roaming rules", posted_date=date(2024, 3, 1)),
            mk("M-MID", title="Wireless roaming rules", posted_date=date(2025, 6, 1)),
            mk("Z-NEW", title="Wireless roaming rules", posted_date=date(2026, 9, 1)),
            mk("Q-OTHER", title="Something unrelated", posted_date=date(2026, 10, 1)),
        ],
        datetime.now(UTC),
    )
    res = await search_cases(
        db_session, CaseFilters(q="roaming", status="all"), sort="relevance", today=date(2026, 11, 23)
    )
    assert [c.notice_number for c in res.cases] == ["Z-NEW", "M-MID", "A-OLD"]


async def test_notice_prefix_ties_break_on_most_recently_posted_first(db_session):
    # Same relevance tier (notice-number prefix match) and same title similarity.
    await upsert_cases(
        db_session,
        [
            mk("2026-300-1", title="Amendment", posted_date=date(2026, 10, 1)),
            mk("2026-300-2", title="Amendment", posted_date=date(2026, 11, 1)),
            mk("2026-300-3", title="Amendment", posted_date=date(2026, 9, 1)),
        ],
        datetime.now(UTC),
    )
    res = await search_cases(
        db_session, CaseFilters(q="2026-300", status="all"), today=date(2026, 11, 23)
    )
    assert [c.notice_number for c in res.cases] == ["2026-300-2", "2026-300-1", "2026-300-3"]
