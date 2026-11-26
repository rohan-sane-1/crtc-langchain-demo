"""Hybrid retrieval over one case's knowledge base: pgvector cosine + Postgres full-text,
fused with reciprocal rank fusion (RRF).

Case isolation is enforced in SQL (``chunks.case_id = :case_id``) on both legs and re-checked
against the document row; the case id comes from the server (runtime context), never the model.
"""

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from langsmith import traceable
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.ingest.embed import Embedder
from app.models import Chunk, Document, DocumentStatus

CANDIDATES = 30
RRF_K = 60
_WORD = re.compile(r"[A-Za-z0-9]{2,}")
_PARA = re.compile(r"\s*(\d{1,5})\.\s")


@dataclass
class Hit:
    chunk_id: int
    document_id: str
    doc_title: str | None
    filer: str | None
    filed_date: str | None
    para_start: int
    para_end: int
    page: int | None
    url: str | None
    text: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def document_link(source_url: str, kind: str | None, detected_type: str | None, page: int | None):
    """User-facing link: no archive-member fragment; PDFs jump to the page when known."""
    if not source_url.startswith("https://"):
        return None
    base = source_url.split("#", 1)[0]
    is_pdf = detected_type == "pdf" or base.lower().endswith(".pdf")
    if kind != "archive_member" and is_pdf and page:
        return f"{base}#page={page}"
    return base


def _or_tsquery(query: str) -> str | None:
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query)))[:24]
    return " | ".join(words) if words else None


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def _rrf(*rankings: list[int]) -> list[int]:
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(score, key=lambda c: (-score[c], c))


@traceable(
    name="retrieval.hybrid_search",
    run_type="retriever",
    process_inputs=lambda i: {k: str(v) for k, v in i.items() if k not in ("session", "embedder")},
)
async def hybrid_search(
    session: AsyncSession,
    embedder: Embedder,
    case_id: uuid.UUID,
    query: str,
    k: int = 8,
    filer: str | None = None,
    rogers_only: bool = False,
) -> list[dict[str, Any]]:
    """Returns LangSmith-style documents: ``{page_content, metadata{...}}``; ``metadata['hit']``
    holds the full ``Hit`` fields. Use ``hits_from_docs`` to get ``Hit`` objects."""
    hits = await _search(session, embedder, case_id, query, k, filer, rogers_only)
    return [
        {
            "page_content": h.text,
            "metadata": {
                "doc_title": h.doc_title,
                "para": f"{h.para_start}-{h.para_end}",
                "url": h.url,
                "filer": h.filer,
                "hit": h.as_dict(),
            },
        }
        for h in hits
    ]


def hits_from_docs(docs: list[dict[str, Any]]) -> list[Hit]:
    return [Hit(**d["metadata"]["hit"]) for d in docs]


async def _search(session, embedder, case_id, query, k, filer, rogers_only) -> list[Hit]:
    k = max(1, min(k, 20))
    base = (
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.case_id == case_id,
            Document.case_id == case_id,
            Document.status == DocumentStatus.embedded,
        )
    )
    if filer:
        base = base.where(Document.filer.ilike(_like(filer)))
    if rogers_only:
        pats = [_like(t) for t in get_settings().rogers_term_list]
        base = base.where(
            or_(*[Document.filer.ilike(p) for p in pats], *[Chunk.text.ilike(p) for p in pats])
        )

    qvec = await embedder.embed_query(query)
    vec_ids = list(
        await session.scalars(
            base.order_by(Chunk.embedding.cosine_distance(qvec)).limit(CANDIDATES)
        )
    )
    fts_ids: list[int] = []
    tsq_text = _or_tsquery(query)
    if tsq_text:
        tsq = func.to_tsquery("english", tsq_text)
        fts_ids = list(
            await session.scalars(
                base.where(Chunk.tsv.op("@@")(tsq))
                .order_by(func.ts_rank(Chunk.tsv, tsq).desc(), Chunk.id)
                .limit(CANDIDATES)
            )
        )
    top = _rrf(vec_ids, fts_ids)[:k]
    if not top:
        return []
    rows = (
        await session.execute(
            select(Chunk, Document)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.id.in_(top), Chunk.case_id == case_id)
        )
    ).all()
    by_id = {c.id: (c, d) for c, d in rows}
    return [_hit(*by_id[cid]) for cid in top if cid in by_id]


def _hit(c: Chunk, d: Document) -> Hit:
    return Hit(
        chunk_id=c.id,
        document_id=str(d.id),
        doc_title=d.title,
        filer=d.filer,
        filed_date=d.filed_date.date().isoformat() if d.filed_date else None,
        para_start=c.para_start,
        para_end=c.para_end,
        page=c.page,
        url=document_link(d.source_url, d.kind, d.detected_type, c.page),
        text=c.text,
    )


async def get_paragraphs(
    session: AsyncSession, document_id: uuid.UUID, case_id: uuid.UUID, para_start: int, para_end: int
) -> dict[str, Any] | None:
    """Text of paragraphs [para_start, para_end] of one document, or None if the document is
    not in `case_id`. Source-numbered chunks start every paragraph with ``N. `` so they are
    sliced exactly; synthetic documents (no inline numbers) return the overlapping chunks."""
    doc = await session.get(Document, document_id)
    if doc is None or doc.case_id != case_id:
        return None
    para_end = min(max(para_end, para_start), para_start + 40)
    chunks = list(
        await session.scalars(
            select(Chunk)
            .where(
                Chunk.document_id == document_id,
                Chunk.case_id == case_id,
                Chunk.para_end >= para_start,
                Chunk.para_start <= para_end,
            )
            .order_by(Chunk.para_start, Chunk.id)
        )
    )
    parts: list[str] = []
    for c in chunks:
        if doc.numbering == "source":
            # Paragraphs are separated by blank lines and each starts with its own "N. ".
            cur: int | None = None
            for piece in re.split(r"\n\s*\n", c.text):
                m = _PARA.match(piece)
                if m:
                    cur = int(m.group(1))
                if cur is not None and para_start <= cur <= para_end:
                    parts.append(piece.strip())
        else:
            parts.append(c.text.strip())
    first = chunks[0] if chunks else None
    return {
        "document_id": str(doc.id),
        "doc_title": doc.title,
        "filer": doc.filer,
        "filed_date": doc.filed_date.date().isoformat() if doc.filed_date else None,
        "para_start": para_start,
        "para_end": para_end,
        "page": first.page if first else None,
        "url": document_link(doc.source_url, doc.kind, doc.detected_type, first.page if first else None),
        "text": "\n\n".join(parts),
    }
