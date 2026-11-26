"""POST /cases/{id}/sync and GET /cases/{id}/sync-status: login, CSRF, rate limit, background run."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import current_user
from app.db import get_session
from app.ingest.status import progress_registry
from app.main import app
from app.models import User
from app.routes import sync as sync_routes
from tests.fakes import APPS, LISTING, fast_parse, filings_payload, make_pdf, make_zip

ORIGIN = {"Origin": "http://localhost:3000"}


@pytest.fixture
async def api(kb_db, make_deps, site, make_case):
    zip_url = f"{APPS}/TransferToWeb/2026/api.zip"
    case = await make_case("2026-500", document_links=[{"url": zip_url, "text": "Docs"}])
    site.add_json(f"{LISTING}?f=2026-500&g=num", filings_payload([]))
    pdfs = {f"{i}.pdf": make_pdf([f"{n}. Paragraph {n} of document {i} for the API test." for n in range(1, 5)]) for i in range(2)}
    site.add_zip(zip_url, make_zip(pdfs))

    async def _session():
        async with kb_db.maker() as s:
            yield s

    users = {"me": User(id=uuid.uuid4(), email="me@example.com"), "you": User(id=uuid.uuid4(), email="you@example.com")}
    who = {"user": users["me"]}
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[current_user] = lambda: who["user"]
    sync_routes.deps_factory = lambda: make_deps(parse=fast_parse)
    sync_routes.sync_limiter.reset()
    progress_registry._items.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.case = case  # type: ignore[attr-defined]
        c.users, c.who = users, who  # type: ignore[attr-defined]
        yield c
    for t in list(sync_routes.background_tasks()):
        await asyncio.gather(t, return_exceptions=True)
    app.dependency_overrides.clear()
    sync_routes.sync_limiter.reset()
    progress_registry._items.clear()


async def drain():
    await asyncio.gather(*sync_routes.background_tasks(), return_exceptions=True)


# ----------------------------------------------------------------------------- auth / CSRF


async def test_sync_endpoints_require_login():
    app.dependency_overrides.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cid = uuid.uuid4()
        assert (await c.post(f"/cases/{cid}/sync", headers=ORIGIN)).status_code == 401
        assert (await c.get(f"/cases/{cid}/sync-status")).status_code == 401


async def test_a_forged_session_cookie_is_not_login(kb_db):
    app.dependency_overrides.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(f"/cases/{uuid.uuid4()}/sync", headers=ORIGIN, cookies={"crtc_session": "forged"})
        assert r.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.example"}, {"Origin": "http://localhost:3000.evil.example"}, {"Origin": "null"}],
)
async def test_post_sync_is_csrf_protected(api, headers):
    r = await api.post(f"/cases/{api.case.id}/sync", headers=headers)
    assert r.status_code == 403
    assert progress_registry.peek(api.case.id) is None  # nothing was started


async def test_get_status_does_not_need_an_origin_header(api):
    assert (await api.get(f"/cases/{api.case.id}/sync-status")).status_code == 200


# ----------------------------------------------------------------------------- rate limit


async def test_sync_is_rate_limited_per_user(api):
    limit = sync_routes.sync_limiter.limit
    for _ in range(limit):
        assert (await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)).status_code == 202
        await drain()
    r = await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    # a different user has their own budget
    api.who["user"] = api.users["you"]
    assert (await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)).status_code == 202
    await drain()


async def test_rate_limited_requests_do_not_start_work(api, site):
    for _ in range(sync_routes.sync_limiter.limit):
        await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)
        await drain()
    n = len(site.log)
    assert (await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)).status_code == 429
    await drain()
    assert len(site.log) == n


# ----------------------------------------------------------------------------- behaviour


async def test_post_starts_a_background_sync_and_status_reports_progress_and_coverage(api):
    r = await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)
    assert r.status_code == 202 and r.json() == {"state": "queued", "started": True}
    await drain()
    st = (await api.get(f"/cases/{api.case.id}/sync-status")).json()
    assert st["state"] == "done"
    assert st["progress"]["phase"] == "done" and st["progress"]["finished_at"]
    assert st["coverage"]["embedded"] == 2 and st["coverage"]["total_docs_on_record"] == 2
    assert st["coverage"]["complete"] is True
    assert st["last_result"]["status"] == "synced" and st["last_result"]["newly_embedded"] == 2


async def test_status_before_any_sync_is_idle_with_empty_coverage(api):
    st = (await api.get(f"/cases/{api.case.id}/sync-status")).json()
    assert st["state"] == "idle" and st["progress"] is None
    assert st["coverage"]["total_docs_on_record"] == 0 and st["coverage"]["complete"] is False


async def test_post_while_a_sync_is_running_does_not_start_a_second(api, site):
    p = progress_registry.get(api.case.id)
    p.start()  # pretend one is already running
    r = await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)
    assert r.status_code == 202 and r.json() == {"state": "listing", "started": False}
    assert site.log == []


async def test_a_second_post_after_completion_checks_again_and_is_up_to_date(api, site):
    await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)
    await drain()
    gets = len(site.document_gets())
    await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)
    await drain()
    st = (await api.get(f"/cases/{api.case.id}/sync-status")).json()
    assert st["last_result"]["status"] == "up_to_date" and len(site.document_gets()) == gets


async def test_a_crashing_sync_is_reported_as_an_error_not_swallowed(api, monkeypatch):
    def broken():
        raise RuntimeError("no model")

    monkeypatch.setattr(sync_routes, "deps_factory", broken)
    await api.post(f"/cases/{api.case.id}/sync", headers=ORIGIN)
    await drain()
    st = (await api.get(f"/cases/{api.case.id}/sync-status")).json()
    assert st["state"] == "error" and st["progress"]["error"] == "RuntimeError"


async def test_unknown_case_is_404_and_bad_id_is_422(api):
    assert (await api.post(f"/cases/{uuid.uuid4()}/sync", headers=ORIGIN)).status_code == 404
    assert (await api.get(f"/cases/{uuid.uuid4()}/sync-status")).status_code == 404
    assert (await api.post("/cases/not-a-uuid/sync", headers=ORIGIN)).status_code == 422


async def test_status_lists_blocked_failed_and_skipped_documents_explicitly(api, site, make_case, kb_db):
    case = await make_case(
        "2026-501",
        source_url="https://crtc.gc.ca/eng/archive/2026/2026-501.htm",
        document_links=[],
    )
    site.add_json(f"{LISTING}?f=2026-501&g=num", filings_payload([]))
    site.add_cloudflare_block("https://crtc.gc.ca/eng/archive/2026/2026-501.htm")
    await api.post(f"/cases/{case.id}/sync", headers=ORIGIN)
    await drain()
    cov = (await api.get(f"/cases/{case.id}/sync-status")).json()["coverage"]
    assert cov["complete"] is False and cov["embedded"] == 0
    assert [b["reason_code"] for b in cov["blocked"]] == ["cloudflare_challenge"]
    assert cov["blocked"][0]["title"] == "Notice 2026-501"


# ----------------------------------------------------------------------------- portal upload


async def test_upload_is_csrf_protected_and_needs_a_file(api):
    url = f"/cases/{api.case.id}/upload"
    files = {"file": ("a.pdf", make_pdf(["1. Hello."]), "application/pdf")}
    assert (await api.post(url, files=files, headers={"Origin": "http://evil.example"})).status_code == 403
    assert (await api.post(url, headers=ORIGIN)).status_code == 422


async def test_upload_ingests_in_the_background_and_reports_the_result(api):
    url = f"/cases/{api.case.id}/upload"
    pdf = make_pdf([f"{n}. Paragraph {n} of an uploaded document." for n in range(1, 5)])
    r = await api.post(url, files={"file": ("Mine.pdf", pdf, "application/pdf")}, headers=ORIGIN)
    assert r.status_code == 202 and r.json()["started"] is True
    await drain()
    st = (await api.get(f"/cases/{api.case.id}/sync-status")).json()
    assert st["last_result"]["status"] == "uploaded" and st["last_result"]["embedded"] == 1
    assert st["coverage"]["embedded"] == 1
    assert [d["title"] for d in st["coverage"]["embedded_docs"]] == ["Mine"]


async def test_upload_rejects_empty_files_and_unknown_cases(api):
    r = await api.post(f"/cases/{api.case.id}/upload", files={"file": ("a.pdf", b"", "application/pdf")}, headers=ORIGIN)
    assert r.status_code == 400
    r = await api.post(f"/cases/{uuid.uuid4()}/upload", files={"file": ("a.pdf", b"x", "application/pdf")}, headers=ORIGIN)
    assert r.status_code == 404
