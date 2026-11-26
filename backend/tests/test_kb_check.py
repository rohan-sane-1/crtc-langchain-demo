# ruff: noqa: F401, F811  (pytest fixture imported from test_pipeline)
"""check_case_missing_documents is read-only and reports what the live record has that the KB lacks."""

from sqlalchemy import func, select

from app.ingest.check import check_case_missing_documents
from app.ingest.pipeline import sync_case_knowledge_base
from app.models import Document
from tests.fakes import fast_parse, filing_row, filings_payload
from tests.test_pipeline import LISTING_URL, scenario


async def _n_docs(kb_db):
    async with kb_db.maker() as s:
        return await s.scalar(select(func.count()).select_from(Document))


async def test_up_to_date_after_sync_and_check_makes_no_writes_or_downloads(
    kb_db, make_deps, scenario, site
):
    case = await scenario()
    deps = make_deps(parse=fast_parse)
    await sync_case_knowledge_base(case.id, deps=deps)
    before_docs, before_gets = await _n_docs(kb_db), len(site.document_gets())

    out = await check_case_missing_documents(case.id, deps)
    assert out["status"] == "up_to_date"
    assert out["missing_count"] == 0 and out["retryable_count"] == 0
    assert out["in_kb_embedded"] == 2
    assert await _n_docs(kb_db) == before_docs
    assert len(site.document_gets()) == before_gets  # listing only; no archive HEADs, no downloads


async def test_new_filing_on_the_record_is_reported_missing(kb_db, make_deps, scenario, site):
    case = await scenario()
    deps = make_deps(parse=fast_parse)
    await sync_case_knowledge_base(case.id, deps=deps)
    site.add_json(
        LISTING_URL,
        filings_payload(
            [
                filing_row(
                    "Doe, Jane",
                    docs=[(101, "Intervention.PDF", "9 KB"), (102, "Intervention.HTML", "2 KB")],
                ),
                filing_row("Roe, Rick", "REPL", docs=[(201, "Reply.docx", "9 KB")]),
                filing_row("New, Nora", docs=[(301, "Late filing.PDF", "9 KB")]),
            ]
        ),
    )
    before = await _n_docs(kb_db)
    out = await check_case_missing_documents(case.id, deps)
    assert out["status"] == "missing_documents" and out["missing_count"] == 1
    assert out["missing"][0]["filer"] and "Late filing" in out["missing"][0]["title"]
    assert await _n_docs(kb_db) == before  # still read-only

    await sync_case_knowledge_base(case.id, deps=deps, force=True)
    assert (await check_case_missing_documents(case.id, deps))["status"] == "up_to_date"


async def test_deleted_row_is_missing_and_removed_filing_is_reported(
    kb_db, make_deps, scenario, site
):
    case = await scenario()
    deps = make_deps(parse=fast_parse)
    await sync_case_knowledge_base(case.id, deps=deps)
    async with kb_db.maker() as s, s.begin():
        d = await s.scalar(select(Document).where(Document.title == "Reply: Reply.docx"))
        await s.delete(d)
    site.add_json(  # Doe's filing vanished from the live record
        LISTING_URL,
        filings_payload([filing_row("Roe, Rick", "REPL", docs=[(201, "Reply.docx", "9 KB")])]),
    )
    out = await check_case_missing_documents(case.id, deps)
    assert out["missing_count"] == 1 and out["removed_count"] >= 1
    assert out["status"] == "missing_documents"


async def test_listing_failure_is_reported_not_raised(kb_db, make_deps, scenario, site):
    case = await scenario()
    deps = make_deps(parse=fast_parse)
    await sync_case_knowledge_base(case.id, deps=deps)
    site.add(LISTING_URL, b"boom", status=500)
    out = await check_case_missing_documents(case.id, deps)
    assert out["status"] == "listing_failed"


async def test_sync_recreates_a_row_that_vanished_even_if_the_record_is_unchanged(
    kb_db, make_deps, scenario, site
):
    case = await scenario()
    deps = make_deps(parse=fast_parse)
    await sync_case_knowledge_base(case.id, deps=deps)
    async with kb_db.maker() as s, s.begin():
        for d in await s.scalars(select(Document).where(Document.kind.in_(["archive", "archive_member"]))):
            await s.delete(d)
    assert (await check_case_missing_documents(case.id, deps))["missing_count"] == 1

    res = await sync_case_knowledge_base(case.id, deps=deps, force=True)
    assert res["status"] == "synced" and res["newly_embedded"] == 2
    assert (await check_case_missing_documents(case.id, deps))["status"] == "up_to_date"
