"""Upsert / cache / filtering / route tests. Need the docker-compose Postgres (each test gets a
throwaway database and is skipped if Postgres is unreachable). No internet needed."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.crtc import cases as cases_mod
from app.crtc.cases import (
    CaseRecord,
    list_crtc_cases,
    parse_cases,
    refresh_cases,
    upsert_cases,
)
from app.crtc.client import FetchFailed
from app.main import app
from app.models import Case, User
from tests.conftest import LIST_URL

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(cases_mod, "_last_refresh", None)
    monkeypatch.setattr(cases_mod, "_default_client", None)


async def count(session) -> int:
    return await session.scalar(select(func.count()).select_from(Case))


# ----------------------------------------------------------------------------- upsert


async def test_upsert_is_idempotent_and_preserves_phase3_columns(db_session, sample_bytes):
    recs = parse_cases(sample_bytes, LIST_URL).records
    assert await upsert_cases(db_session, recs, T0) == len(recs)
    n = await count(db_session)
    ids = dict((await db_session.execute(select(Case.notice_number, Case.id))).all())

    # Phase 3 owns these columns; a re-list must not clobber them.
    await db_session.execute(update(Case).values(record_fingerprint="fp", last_synced_at=T0))
    await db_session.commit()

    later = T0 + timedelta(hours=2)
    await upsert_cases(db_session, recs, later)
    assert await count(db_session) == n == len(recs)
    ids2 = dict((await db_session.execute(select(Case.notice_number, Case.id))).all())
    assert ids == ids2  # same rows updated in place
    row = await db_session.scalar(select(Case).where(Case.notice_number == "2026-259"))
    assert row.last_listed_at == later
    assert row.record_fingerprint == "fp" and row.last_synced_at == T0


async def test_upsert_updates_changed_fields(db_session, sample_bytes):
    recs = parse_cases(sample_bytes, LIST_URL).records
    await upsert_cases(db_session, recs, T0)
    changed = [r.model_copy(update={"title": "New title", "status": "closed"}) for r in recs]
    await upsert_cases(db_session, changed, T0)
    row = await db_session.scalar(select(Case).where(Case.notice_number == "2026-259"))
    assert (row.title, row.status) == ("New title", "closed")


async def test_upsert_runs_in_one_transaction_and_rolls_back(db_session, monkeypatch):
    monkeypatch.setattr(cases_mod, "UPSERT_BATCH", 2)

    def rec(n, title="t"):
        return CaseRecord(
            notice_number=f"n{n}", title=title, status="open", posted_date="2026-01-01"
        )

    # Batch 1 (n0, n1) is fine; batch 2 contains a NUL byte which Postgres rejects.
    batch = [rec(0), rec(1), rec(2, title="bad\x00nul"), rec(3)]
    with pytest.raises(Exception):  # noqa: B017 - driver-specific error type
        await upsert_cases(db_session, batch, T0)
    await db_session.rollback()
    assert await count(db_session) == 0  # nothing from batch 1 survived


async def test_upsert_empty_is_noop(db_session):
    assert await upsert_cases(db_session, [], T0) == 0


# ----------------------------------------------------------------------------- cache


async def test_cache_prevents_second_fetch_within_ttl(db_session, list_client):
    r1 = await refresh_cases(db_session, list_client, ttl=timedelta(minutes=60), now=T0)
    assert r1.fetched and len(list_client.seen) == 1 and r1.upserted == 13
    r2 = await refresh_cases(
        db_session, list_client, ttl=timedelta(minutes=60), now=T0 + timedelta(minutes=59)
    )
    assert not r2.fetched and not r2.stale
    assert len(list_client.seen) == 1  # no second network request
    r3 = await refresh_cases(
        db_session, list_client, ttl=timedelta(minutes=60), now=T0 + timedelta(minutes=61)
    )
    assert r3.fetched and len(list_client.seen) == 2  # TTL elapsed -> exactly one more fetch


async def test_cache_force_refetches(db_session, list_client):
    await refresh_cases(db_session, list_client, now=T0)
    await refresh_cases(db_session, list_client, now=T0, force=True)
    assert len(list_client.seen) == 2


async def test_cache_default_ttl_comes_from_settings(db_session, list_client):
    await refresh_cases(db_session, list_client)  # real clock
    await refresh_cases(db_session, list_client)
    assert len(list_client.seen) == 1


async def test_concurrent_callers_share_one_fetch(db_session, list_client):
    maker = db_session.maker

    async def call():
        async with maker() as s:
            return await refresh_cases(s, list_client)

    results = await asyncio.gather(*(call() for _ in range(5)))
    assert len(list_client.seen) == 1
    assert sum(r.fetched for r in results) == 1


async def test_failed_refresh_serves_stale_data(db_session, list_client, make_client):
    await refresh_cases(db_session, list_client, now=T0)
    broken = make_client(lambda r: httpx.Response(500), max_retries=0)
    res = await refresh_cases(db_session, broken, now=T0 + timedelta(hours=5))
    assert not res.fetched and res.stale and res.error
    assert await count(db_session) == 13  # old data untouched


async def test_garbage_response_does_not_touch_db(db_session, make_client):
    client = make_client(lambda r: httpx.Response(200, content=b'{"data": [1, 2, 3]}'))
    res = await refresh_cases(db_session, client, now=T0)
    assert res.stale and await count(db_session) == 0


async def test_list_crtc_cases_reports_coverage_and_drops(db_session, list_client):
    out = await list_crtc_cases(db_session, status="open", client=list_client)
    assert out["total_cases_listed"] == 13
    assert out["matched"] == out["returned"] == len(out["cases"]) == 4
    assert out["refreshed"] and not out["stale"]
    assert out["dropped_records"] == 1  # the duplicate notice number in the sample
    top = out["cases"][0]
    assert set(top) >= {"id", "notice_number", "title", "status", "posted_date"}
    again = await list_crtc_cases(db_session, status="open", client=list_client)
    assert not again["refreshed"] and again["dropped_records"] == 1  # remembered from last fetch
    assert len(list_client.seen) == 1


# ----------------------------------------------------------------------------- route


@pytest.fixture
async def api(db_session, list_client, monkeypatch):
    from app.auth import current_user
    from app.db import get_session

    monkeypatch.setattr(cases_mod, "_default_client", list_client)

    async def _session():
        async with db_session.maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[current_user] = lambda: User(id=uuid.uuid4(), email="a@b.c")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_cases_route_requires_login():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/cases")).status_code == 401


async def test_cases_route_lists_and_filters(api):
    r = await api.get("/cases")
    assert r.status_code == 200
    body = r.json()
    assert body["total_cases_listed"] == 13 and body["matched"] == 4
    assert all(c["status"] == "open" for c in body["cases"])
    r = await api.get("/cases", params={"q": "red tape", "status": "all", "sector": "telecom"})
    assert [c["notice_number"] for c in r.json()["cases"]] == ["2026-259"]


@pytest.mark.parametrize(
    "params",
    [
        {"status": "bogus"},
        {"status": "open' OR 1=1"},
        {"sector": "bogus"},
        {"page_size": 101},
        {"page_size": 0},
        {"page_size": "abc"},
        {"q": "x" * 201},
    ],
)
async def test_cases_route_rejects_invalid_params(api, params):
    assert (await api.get("/cases", params=params)).status_code == 422


async def test_cases_route_sql_injection_attempt_is_harmless(api):
    r = await api.get("/cases", params={"q": "'; DROP TABLE cases; --"})
    assert r.status_code == 200 and r.json()["matched"] == 0
    assert (await api.get("/cases")).json()["total_cases_listed"] == 13


async def test_cases_route_503_when_nothing_cached_and_fetch_fails(
    db_session, make_client, monkeypatch
):
    from app.auth import current_user
    from app.db import get_session

    monkeypatch.setattr(
        cases_mod, "_default_client", make_client(lambda r: httpx.Response(500), max_retries=0)
    )

    async def _session():
        async with db_session.maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[current_user] = lambda: User(id=uuid.uuid4(), email="a@b.c")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/cases")).status_code == 503
    finally:
        app.dependency_overrides.clear()
    assert FetchFailed  # imported for clarity of intent
