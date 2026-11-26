"""Retrieval tools. Output is wrapped in <source_text> delimiters: it is untrusted filing text."""

import uuid

from langchain.tools import ToolRuntime, tool

from app.agent import retrieval
from app.agent.context import CaseContext
from app.db import SessionLocal
from app.ingest import runner


def _wrap(cid: str, h: retrieval.Hit) -> str:
    body = h.text.replace("</source_text>", "")
    attrs = (
        f'id="{cid}" doc_id="{h.document_id}" title="{(h.doc_title or "").replace(chr(34), "")}" '
        f'filer="{(h.filer or "unknown").replace(chr(34), "")}" filed="{h.filed_date or "unknown"}" '
        f'paragraphs="{h.para_start}-{h.para_end}" page="{h.page or ""}"'
    )
    return f"<source_text {attrs}>\n{body}\n</source_text>"


@tool
async def search_case_documents(
    query: str,
    runtime: ToolRuntime[CaseContext],
    k: int = 8,
    filer: str | None = None,
    rogers_only: bool = False,
) -> str:
    """Search this case's documents (hybrid semantic + keyword). Returns passages as
    <source_text id="cN" ...> blocks; cite them in the answer as [cN].

    Args:
        query: what to look for, phrased like the passage you expect (not the user's whole question).
        k: number of passages (1-20).
        filer: optional filter: only documents whose filer name contains this text.
        rogers_only: only passages from Rogers filings or that mention Rogers.
    """
    ctx = runtime.context
    try:
        async with SessionLocal() as session:
            docs = await retrieval.hybrid_search(
                session, runner.deps_factory().embedder, ctx.case_id, query, k, filer, rogers_only
            )
    except Exception as e:  # surfaced to the model and to LangSmith, not a crash
        return f"search failed: {type(e).__name__}"
    hits = retrieval.hits_from_docs(docs)
    if not hits:
        return "No matching passages in this case's knowledge base."
    return "\n\n".join(_wrap(ctx.register(h), h) for h in hits)


@tool
async def get_paragraphs(
    document_id: str, para_start: int, para_end: int, runtime: ToolRuntime[CaseContext]
) -> str:
    """Read specific paragraphs of one document in this case (use the doc_id from a search result),
    e.g. to see the text around a hit. At most 40 paragraphs. Cite as [cN]."""
    ctx = runtime.context
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        return "invalid document_id"
    async with SessionLocal() as session:
        got = await retrieval.get_paragraphs(session, doc_uuid, ctx.case_id, para_start, para_end)
    if got is None or not got["text"]:
        return "No such document/paragraphs in this case."
    hit = retrieval.Hit(
        chunk_id=0,
        document_id=got["document_id"],
        doc_title=got["doc_title"],
        filer=got["filer"],
        filed_date=got["filed_date"],
        para_start=got["para_start"],
        para_end=got["para_end"],
        page=got["page"],
        url=got["url"],
        text=got["text"],
    )
    return _wrap(ctx.register(hit), hit)
