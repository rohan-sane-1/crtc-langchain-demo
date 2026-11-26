# ruff: noqa: F401, F811  (pytest fixture imported from test_pipeline)
import uuid

from app.agent import retrieval
from app.ingest.pipeline import sync_case_knowledge_base
from app.models import Case, Chunk, Document, DocumentStatus
from tests.fakes import fast_parse
from tests.test_pipeline import scenario


async def _add_doc(kb_db, embedder, case_id, *, title, filer, paras, url):
    texts = [f"{i}. {t}" for i, t in enumerate(paras, 1)]
    vecs = await embedder.embed_passages(texts)
    async with kb_db.maker() as s, s.begin():
        d = Document(
            case_id=case_id,
            source_url=url,
            title=title,
            filer=filer,
            kind="filing",
            status=DocumentStatus.embedded,
            numbering="source",
            detected_type="pdf",
        )
        s.add(d)
        await s.flush()
        for i, (t, v) in enumerate(zip(texts, vecs, strict=True), 1):
            s.add(
                Chunk(
                    document_id=d.id, case_id=case_id, para_start=i, para_end=i, page=2,
                    text=t, embedding=v,
                )
            )
    return d.id


async def _case(kb_db, notice):
    async with kb_db.maker() as s, s.begin():
        c = Case(notice_number=notice, title=notice)
        s.add(c)
    return c.id


async def test_case_isolation_and_rogers_filter(kb_db, embedder):
    a, b = await _case(kb_db, "A-1"), await _case(kb_db, "B-1")
    await _add_doc(
        kb_db, embedder, a, title="Rogers submission", filer="Rogers Communications",
        paras=["Wholesale roaming rates should fall.", "Unrelated procedural text."],
        url="https://applications.crtc.gc.ca/x/a1.pdf",
    )
    await _add_doc(
        kb_db, embedder, a, title="Bell letter", filer="Bell Canada",
        paras=["Wholesale roaming rates must stay.", "Rogers is wrong about rates."],
        url="https://applications.crtc.gc.ca/x/a2.pdf",
    )
    await _add_doc(
        kb_db, embedder, b, title="Other case", filer="Rogers Communications",
        paras=["Wholesale roaming rates in another proceeding."],
        url="https://applications.crtc.gc.ca/x/b1.pdf",
    )
    async with kb_db.maker() as s:
        hits = await retrieval._search(s, embedder, a, "wholesale roaming rates", 10, None, False)
        assert hits and {h.doc_title for h in hits} <= {"Rogers submission", "Bell letter"}
        assert "Other case" not in {h.doc_title for h in hits}  # case isolation

        rogers = await retrieval._search(s, embedder, a, "wholesale roaming rates", 10, None, True)
        titles = {(h.doc_title, h.para_start) for h in rogers}
        assert ("Rogers submission", 1) in titles and ("Bell letter", 2) in titles  # filer OR mention
        assert ("Bell letter", 1) not in titles  # Bell paragraph that never mentions Rogers

        by_filer = await retrieval._search(s, embedder, a, "rates", 10, "bell", False)
        assert {h.doc_title for h in by_filer} == {"Bell letter"}


async def test_hybrid_search_traceable_shape_and_links(kb_db, embedder):
    a = await _case(kb_db, "A-2")
    await _add_doc(
        kb_db, embedder, a, title="T", filer="F", paras=["Spectrum auction timing."],
        url="https://applications.crtc.gc.ca/x/t.pdf",
    )
    async with kb_db.maker() as s:
        docs = await retrieval.hybrid_search(s, embedder, a, "spectrum auction")
    meta = docs[0]["metadata"]
    assert docs[0]["page_content"].startswith("1. Spectrum") and meta["para"] == "1-1"
    hit = retrieval.hits_from_docs(docs)[0]
    assert hit.url == "https://applications.crtc.gc.ca/x/t.pdf#page=2"


def test_document_link_rules():
    f = retrieval.document_link
    assert f("https://a.crtc.gc.ca/z.zip#m=abc", "archive_member", None, 3) == "https://a.crtc.gc.ca/z.zip"
    assert f("https://a.crtc.gc.ca/x.pdf", "filing", "pdf", 4) == "https://a.crtc.gc.ca/x.pdf#page=4"
    assert f("https://a.crtc.gc.ca/x.pdf", "filing", "pdf", None) == "https://a.crtc.gc.ca/x.pdf"
    assert f("http://insecure/x.pdf", "filing", "pdf", 1) is None


async def test_get_paragraphs_slices_and_enforces_case(kb_db, embedder):
    a, b = await _case(kb_db, "A-3"), await _case(kb_db, "B-3")
    text = "1. First.\n\n2. Second para\nwith two lines.\n\n3. Third."  # like the real chunker
    async with kb_db.maker() as s, s.begin():
        d = Document(
            case_id=a, source_url="https://applications.crtc.gc.ca/x/p.pdf", title="P",
            kind="filing", status=DocumentStatus.embedded, numbering="source",
        )
        s.add(d)
        await s.flush()
        vec = (await embedder.embed_passages([text]))[0]
        s.add(Chunk(document_id=d.id, case_id=a, para_start=1, para_end=3, page=1, text=text, embedding=vec))
    async with kb_db.maker() as s:
        got = await retrieval.get_paragraphs(s, d.id, a, 2, 3)
        assert got["text"] == "2. Second para\nwith two lines.\n\n3. Third."
        assert await retrieval.get_paragraphs(s, d.id, b, 1, 3) is None  # wrong case
        assert await retrieval.get_paragraphs(s, uuid.uuid4(), a, 1, 3) is None


async def test_search_on_synced_case_returns_embedded_docs(kb_db, make_deps, scenario, embedder):
    case = await scenario()
    await sync_case_knowledge_base(case.id, deps=make_deps(parse=fast_parse))
    async with kb_db.maker() as s:
        hits = await retrieval._search(s, embedder, case.id, "applicant states tariff change", 5, None, False)
    assert hits and all(h.doc_title for h in hits)
