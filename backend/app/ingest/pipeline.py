"""Knowledge-base sync: record listing -> diff -> download -> type-check -> parse -> chunk ->
embed -> one transaction per document.

Guarantees (each is covered by tests/test_pipeline.py unless noted):

* **Cheap when nothing changed.** Within ``kb_recheck_min_interval_s`` of the last sync, or
  when the record fingerprint is unchanged and no document needs a retry, the result is
  ``up_to_date`` with zero downloads and zero embeddings.
* **Incremental.** Only new/changed documents are fetched. An unchanged SHA-256 is never
  re-embedded; a changed document has its old chunks replaced atomically.
* **One transaction per document.** Chunks are written, and the document row finalised, in a
  single transaction. A failure rolls it back: no half-written chunks, and the previous
  chunks (if any) are left intact. The failure is then recorded on the document.
* **One sync per case at a time** (Postgres advisory lock): a second caller waits up to
  ``wait_s`` and then gets ``in_progress``.
* **Nothing is silently dropped.** Every document on the record ends in exactly one state:
  embedded / pending / failed (with reason) / skipped (with reason) / blocked (with reason).
* **Blocked means blocked.** robots.txt, 401/403/451, 429 and Cloudflare challenges are
  recorded (``blocked``) and not retried within the run; once a host blocks us in a run,
  its remaining documents are marked blocked without sending further requests.
* **Failures are isolated.** An unexpected exception in one document marks that document
  failed; the sync continues. Document text is never logged.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.crtc import records
from app.crtc.client import (
    CrtcClient,
    CrtcClientError,
    FetchBlocked,
    FetchFailed,
    ResponseTooLarge,
)
from app.crtc.html import normalize_text
from app.ingest import filetype
from app.ingest.archive import ArchiveLimits, SafeZip, UnsafeArchive
from app.ingest.chunk import ChunkOut, build_paragraphs, chunk_paragraphs
from app.ingest.embed import Embedder
from app.ingest.parse import ParsedDocument, ParseFailed, parse_file_async
from app.ingest.status import Progress, coverage, progress_registry
from app.ingest.store import OriginalsStore
from app.models import Case, Chunk, Document, DocumentStatus

log = logging.getLogger(__name__)

LOCK_NAMESPACE = 0x43525443  # "CRTC": first key of the two-int4 advisory lock
# Reasons that will not change by retrying the same bytes. Anything else failed->retryable.
PERMANENT_FAILURES = frozenset(
    {
        "needs_ocr",
        "encrypted",
        "malformed",
        "type_mismatch",
        "unsafe_archive",
        "too_many_pages",
        "too_much_text",
        "too_large",
        "empty",
        "not_found",
        "timeout",
        "crashed",
        "out_of_memory",
        "not_an_archive",
    }
)

ParseFn = Callable[[Path, str], Awaitable[ParsedDocument]]


class CaseNotFound(Exception):
    pass


REASON_TEXT = {
    "needs_ocr": "scanned PDF with no text layer; OCR is not supported",
    "encrypted": "password-protected",
    "malformed": "the file is corrupt or could not be read",
    "timeout": "the parser timed out",
    "crashed": "the parser crashed on this file",
    "out_of_memory": "the parser ran out of memory",
    "too_many_pages": "too many pages",
    "too_much_text": "too much text",
    "too_large": "file too large",
    "empty": "no text found",
    "sandbox_unavailable": "the parsing sandbox could not be started",
}


def parse_reason(code: str) -> str:
    return f"{code}: {REASON_TEXT[code]}" if code in REASON_TEXT else code


def error_code(error: str | None) -> str:
    return (error or "").split(":", 1)[0].strip()


def is_retryable(error: str | None) -> bool:
    return error_code(error) not in PERMANENT_FAILURES


@dataclass
class Deps:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    client: CrtcClient
    embedder: Embedder
    store: OriginalsStore
    parse: ParseFn = parse_file_async
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


_default_embedder: Embedder | None = None


def default_deps() -> Deps:
    from app.crtc.cases import get_default_client
    from app.db import SessionLocal, engine
    from app.ingest.embed import SentenceTransformerEmbedder

    global _default_embedder
    if _default_embedder is None:
        _default_embedder = SentenceTransformerEmbedder()
    return Deps(
        engine=engine,
        session_factory=SessionLocal,
        client=get_default_client(),
        embedder=_default_embedder,
        store=OriginalsStore(),
    )


# --------------------------------------------------------------------------- advisory lock


class CaseLock:
    """Session-level Postgres advisory lock held on a dedicated connection."""

    def __init__(self, engine: AsyncEngine, case_id: uuid.UUID) -> None:
        self._engine = engine
        self._keys = {
            "a": LOCK_NAMESPACE,
            "b": int.from_bytes(case_id.bytes[:4], "big", signed=True),
        }
        self._conn = None

    async def acquire(self, wait_s: float = 0.0) -> bool:
        conn = await self._engine.connect()
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait_s)
        try:
            while True:
                got = (
                    await conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), self._keys)
                ).scalar()
                if got:
                    self._conn = conn
                    return True
                if loop.time() >= deadline:
                    await conn.close()
                    return False
                await asyncio.sleep(0.2)
        except BaseException:
            await conn.invalidate()  # drops the DB session, and with it any lock we may hold
            raise

    async def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), self._keys)
            await conn.close()
        except Exception:  # noqa: BLE001 - a broken connection must not leak the lock
            await conn.invalidate()


# --------------------------------------------------------------------------- run context


@dataclass
class Run:
    deps: Deps
    case_id: uuid.UUID
    now: datetime
    progress: Progress
    listing: dict[str, records.RecordDoc] = field(default_factory=dict)
    blocked_hosts: dict[str, str] = field(default_factory=dict)  # host -> reason (this run only)
    newly_embedded: int = 0
    unchanged: int = 0


def _s():
    return get_settings()


# --------------------------------------------------------------------------- db helpers


async def _mark(
    run: Run,
    doc_id: uuid.UUID,
    status: DocumentStatus,
    error: str | None = None,
    **fields,
) -> None:
    async with run.deps.session_factory() as s, s.begin():
        await s.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(status=status, error=error, attempted_at=run.now, **fields)
        )


async def _write_chunks(
    session: AsyncSession,
    doc_id: uuid.UUID,
    case_id: uuid.UUID,
    chunks: list[ChunkOut],
    vectors: list[list[float]],
) -> None:
    await session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
    rows = [
        {
            "document_id": doc_id,
            "case_id": case_id,
            "para_start": c.para_start,
            "para_end": c.para_end,
            "page": c.page,
            "text": c.text,
            "embedding": v,
        }
        for c, v in zip(chunks, vectors, strict=True)
    ]
    for i in range(0, len(rows), 200):
        await session.execute(insert(Chunk), rows[i : i + 200])


async def _commit_embedded(
    run: Run,
    doc: Document,
    chunks: list[ChunkOut],
    vectors: list[list[float]],
    **fields,
) -> None:
    """The one-transaction-per-document write: chunks + document row, atomically."""
    async with run.deps.session_factory() as s, s.begin():
        await _write_chunks(s, doc.id, doc.case_id, chunks, vectors)
        await s.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(
                status=DocumentStatus.embedded,
                error=None,
                attempted_at=run.now,
                fetched_at=run.now,
                **fields,
            )
        )


async def _has_chunks(deps: Deps, doc_id: uuid.UUID) -> bool:
    async with deps.session_factory() as s:
        n = await s.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc_id))
    return bool(n)


# --------------------------------------------------------------------------- one document


def _display_title(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return (" ".join(normalize_text(base).split()) or "Document")[:300]


def member_url(archive_url: str, name: str) -> str:
    h = hashlib.sha1(name.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:16]
    return f"{archive_url}#m={h}"


async def _ingest_bytes(
    run: Run,
    doc: Document,
    data: bytes,
    *,
    claimed: str | None,
    signature: str | None = None,
    allow_archive: bool = False,
) -> None:
    """Type-check, store, parse, chunk, embed and commit `data` as the content of `doc`."""
    deps = run.deps
    detected = filetype.detect_type(data)
    sig = {"source_signature": signature} if signature is not None else {}
    meta = {"mime": detected.mime, "detected_type": detected.type, "bytes": len(data)}

    mismatch = filetype.check_type(detected, claimed)
    if mismatch:
        await _mark(run, doc.id, DocumentStatus.failed, mismatch, **meta)
        return
    if detected.type == filetype.ZIP and not allow_archive:
        await _mark(run, doc.id, DocumentStatus.skipped, "nested_archive: not extracted", **meta)
        return
    if detected.type not in filetype.PARSEABLE:
        await _mark(
            run, doc.id, DocumentStatus.skipped, f"unsupported_type: {detected.type}", **meta
        )
        return

    sha = deps.store.save(data)
    same_bytes = doc.sha256 == sha and doc.status in (DocumentStatus.pending, DocumentStatus.embedded)
    if same_bytes and await _has_chunks(deps, doc.id):  # nothing to re-embed
        run.unchanged += 1
        await _mark(run, doc.id, DocumentStatus.embedded, None, fetched_at=run.now, **sig)
        return

    try:
        parsed = await deps.parse(deps.store.path_for(sha), detected.type)
    except ParseFailed as e:
        await _mark(run, doc.id, DocumentStatus.failed, parse_reason(e.code), sha256=sha, **meta)
        return
    paragraphs, numbering = build_paragraphs(parsed.blocks)
    if not paragraphs:
        await _mark(run, doc.id, DocumentStatus.failed, parse_reason("empty"), sha256=sha, **meta)
        return
    chunks = chunk_paragraphs(paragraphs, numbering)
    vectors = await deps.embedder.embed_passages([c.text for c in chunks])
    if len(vectors) != len(chunks) or any(len(v) != deps.embedder.dim for v in vectors):
        raise RuntimeError("embedder returned unexpected vectors")

    note = None
    if parsed.pages_without_text and parsed.pages:
        note = (
            f"{parsed.pages_without_text} of {parsed.pages} pages have no text layer "
            "(scanned); those pages need OCR and are not searchable"
        )
    await _commit_embedded(
        run,
        doc,
        chunks,
        vectors,
        sha256=sha,
        n_paragraphs=len(paragraphs),
        numbering=numbering,
        note=note,
        **meta,
        **sig,
    )
    run.newly_embedded += 1


def _blocked_message(e: FetchBlocked) -> str:
    if e.reason == "robots_disallowed":
        rule = getattr(e, "rule", None)
        return f"robots_disallowed: robots.txt disallows this path{f' ({rule})' if rule else ''}"
    return f"{e.reason}: HTTP {e.status_code}" if e.status_code else e.reason


async def _download(run: Run, doc: Document):
    """GET the document, or record why not. Returns the FetchResult or None."""
    host = (urlsplit(doc.source_url).hostname or "").lower()
    if host in run.blocked_hosts:  # already refused this run: don't ask again
        await _mark(
            run,
            doc.id,
            DocumentStatus.blocked,
            f"{run.blocked_hosts[host]}: host refused earlier in this sync; not retried",
        )
        return None
    try:
        return await run.deps.client.fetch_document(doc.source_url)
    except FetchBlocked as e:
        if e.reason != "robots_disallowed":
            run.blocked_hosts[host] = e.reason
        await _mark(run, doc.id, DocumentStatus.blocked, _blocked_message(e))
    except ResponseTooLarge:
        await _mark(run, doc.id, DocumentStatus.failed, "too_large: over the download size cap")
    except FetchFailed as e:
        if e.status_code in (404, 410):
            await _mark(run, doc.id, DocumentStatus.failed, f"not_found: HTTP {e.status_code}")
        else:
            code = f"HTTP {e.status_code}" if e.status_code else "network error"
            await _mark(run, doc.id, DocumentStatus.failed, f"fetch_failed: {code}")
    except CrtcClientError as e:
        await _mark(run, doc.id, DocumentStatus.failed, f"fetch_failed: {type(e).__name__}")
    return None


async def _process_plain(run: Run, doc: Document) -> None:
    rd = run.listing.get(doc.source_url)
    signature = rd.signature if rd else None
    # Re-parsing a document we already hold (retry after a parse/embed failure) needs no download.
    if (
        doc.sha256
        and doc.status == DocumentStatus.failed
        and is_retryable(doc.error)
        and run.deps.store.has(doc.sha256)
        and not (signature and signature != doc.source_signature)
    ):
        await _ingest_bytes(run, doc, run.deps.store.read(doc.sha256), claimed=None)
        return
    resp = await _download(run, doc)
    if resp is None:
        return
    claimed = filetype.claimed_type(doc.source_url, resp.content_type)
    await _ingest_bytes(run, doc, resp.content, claimed=claimed, signature=signature)


async def _process_archive(run: Run, doc: Document) -> None:
    deps = run.deps
    rd = run.listing.get(doc.source_url)
    resp = await _download(run, doc)
    if resp is None:
        return
    data = resp.content
    detected = filetype.detect_type(data)
    mismatch = filetype.check_type(detected, filetype.claimed_type(doc.source_url, resp.content_type))
    if mismatch:
        await _mark(run, doc.id, DocumentStatus.failed, mismatch, bytes=len(data))
        return
    if detected.type != filetype.ZIP:
        await _mark(run, doc.id, DocumentStatus.failed, "not_an_archive: not a ZIP file")
        return
    sha = deps.store.save(data)
    s = _s()
    limits = ArchiveLimits(
        max_entries=s.kb_max_zip_entries,
        max_total_bytes=s.kb_max_zip_uncompressed_bytes,
        max_member_bytes=s.crtc_max_document_bytes,
        max_ratio=s.kb_max_zip_ratio,
    )

    def open_zip() -> SafeZip:
        return SafeZip(data, limits)

    try:
        zf = await asyncio.to_thread(open_zip)
    except UnsafeArchive as e:
        await _mark(run, doc.id, DocumentStatus.failed, f"unsafe_archive: {e.reason}", sha256=sha)
        return

    async with deps.session_factory() as sess:
        prefix = doc.source_url + "#m="
        existing = {
            d.source_url: d
            for d in await sess.scalars(
                select(Document).where(
                    Document.case_id == doc.case_id, Document.source_url.startswith(prefix, autoescape=True)
                )
            )
        }
    seen: set[str] = set()
    try:
        for entry in zf.entries:
            url = member_url(doc.source_url, entry.name)
            seen.add(url)
            sig = f"{entry.crc:08x}:{entry.size}"
            row = existing.get(url)
            if row is None:
                row = await _create_member(run, doc, url, entry.name)
            if entry.skip_reason:
                await _mark(run, row.id, DocumentStatus.skipped, entry.skip_reason, source_signature=sig)
                continue
            same = row.source_signature == sig
            if same and row.status == DocumentStatus.embedded:
                continue
            if (
                same
                and row.status in (DocumentStatus.failed, DocumentStatus.skipped)
                and not is_retryable(row.error)
            ):
                continue
            run.progress.current = row.title
            try:
                member = await asyncio.to_thread(zf.read, entry)
            except UnsafeArchive as e:  # the archive lied about its sizes: refuse all of it
                await _mark(
                    run, doc.id, DocumentStatus.failed, f"unsafe_archive: {e.reason}", sha256=sha
                )
                return
            await _guarded(
                run, row, _ingest_bytes, member, claimed=filetype.claimed_type("", None, entry.name),
                signature=sig,
            )
    finally:
        zf.close()

    gone = [d.id for u, d in existing.items() if u not in seen]
    if gone:
        async with deps.session_factory() as sess, sess.begin():
            await sess.execute(delete(Document).where(Document.id.in_(gone)))
    n = len(seen)
    await _mark(
        run,
        doc.id,
        DocumentStatus.embedded,
        None,
        sha256=sha,
        bytes=len(data),
        mime=detected.mime,
        detected_type=detected.type,
        fetched_at=run.now,
        note=f"archive with {n} file{'s' if n != 1 else ''}",
        source_signature=rd.signature if rd else None,
    )


async def _create_member(run: Run, archive: Document, url: str, name: str) -> Document:
    row = Document(
        case_id=archive.case_id,
        source_url=url,
        title=_display_title(name),
        filer=archive.filer,
        filed_date=archive.filed_date,
        kind="archive_member",
        status=DocumentStatus.pending,
    )
    async with run.deps.session_factory() as s, s.begin():
        s.add(row)
    return row


async def _guarded(run: Run, doc: Document, fn, *args, **kw) -> None:
    """Run one document's work; any unexpected error costs that document, never the sync."""
    try:
        await fn(run, doc, *args, **kw)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("document processing failed (%s)", type(e).__name__)  # no document text
        try:
            await _mark(run, doc.id, DocumentStatus.failed, f"internal_error: {type(e).__name__}")
        except Exception:
            log.exception("could not record failure")


async def _process(run: Run, doc: Document) -> None:
    run.progress.current = doc.title
    await _guarded(run, doc, _process_archive if doc.kind == "archive" else _process_plain)


# --------------------------------------------------------------------------- planning


def _needs_work(row: Document, rd: records.RecordDoc | None, now: datetime) -> bool:
    s = _s()
    sig_changed = bool(rd and rd.signature and rd.signature != row.source_signature)
    st = row.status
    if st == DocumentStatus.pending or sig_changed:
        return True
    if st == DocumentStatus.failed:
        return is_retryable(row.error)
    if st == DocumentStatus.blocked:
        if error_code(row.error) == "robots_disallowed":
            return True  # costs nothing: robots.txt is cached; allowed if the rules changed
        due = row.attempted_at is None or now - row.attempted_at >= timedelta(
            seconds=s.kb_blocked_retry_s
        )
        return due
    if st == DocumentStatus.embedded and row.kind == "archive" and not (rd and rd.signature):
        # No size/date/etag to compare (HEAD failed): re-check at most once per retry window.
        return row.attempted_at is None or now - row.attempted_at >= timedelta(
            seconds=s.kb_blocked_retry_s
        )
    return False


def _fast_path_blocker(rows: list[Document], now: datetime) -> bool:
    """True if some document needs network work, so 'up to date' must not be claimed."""
    s = _s()
    for r in rows:
        if r.status == DocumentStatus.pending:
            return True
        if r.status == DocumentStatus.failed and is_retryable(r.error):
            return True
        if (
            r.status == DocumentStatus.blocked
            and error_code(r.error) != "robots_disallowed"
            and (r.attempted_at is None or now - r.attempted_at >= timedelta(seconds=s.kb_blocked_retry_s))
        ):
            return True
    return False


async def _plan(run: Run, listing: records.RecordListing) -> list[uuid.UUID]:
    """Make the documents table match the listing; return ids that need work (archives first)."""
    docs = listing.documents
    run.listing = {d.url: d for d in docs}
    urls = set(run.listing)
    async with run.deps.session_factory() as s, s.begin():
        existing = {
            d.source_url: d
            for d in await s.scalars(select(Document).where(Document.case_id == run.case_id))
        }
        # Gone from the record (or from its archive): remove, chunks cascade.
        gone = []
        for url, d in existing.items():
            if d.kind == "local_file":
                continue  # added by hand (ingest-file); not part of any listing
            if d.kind == "filing" and not listing.listing_ok:
                continue  # the filings endpoint failed: absence proves nothing
            base = url.split("#", 1)[0] if d.kind == "archive_member" else url
            if base not in urls:
                gone.append(d.id)
        if gone:
            await s.execute(delete(Document).where(Document.id.in_(gone)))
            existing = {u: d for u, d in existing.items() if d.id not in set(gone)}

        for rd in docs:
            d = existing.get(rd.url)
            if d is None:
                d = Document(
                    case_id=run.case_id,
                    source_url=rd.url,
                    status=DocumentStatus.pending,
                )
                s.add(d)
                existing[rd.url] = d
            d.title, d.filer, d.filed_date, d.kind = rd.title, rd.filer, _dt(rd.filed_date), rd.kind
            if rd.variant_of and rd.variant_of in urls:
                d.status = DocumentStatus.skipped
                d.error = "duplicate_format: same filing is listed as a PDF"
        await s.flush()

        # Archive members that need a retry make the archive itself due again.
        retry_parents: set[str] = set()
        for d in existing.values():
            if d.kind == "archive_member" and (
                d.status == DocumentStatus.pending
                or (d.status == DocumentStatus.failed and is_retryable(d.error))
            ):
                retry_parents.add(d.source_url.split("#", 1)[0])

        work: list[Document] = []
        for rd in docs:
            d = existing[rd.url]
            if d.status == DocumentStatus.skipped and error_code(d.error) == "duplicate_format":
                continue
            if _needs_work(d, rd, run.now) or rd.url in retry_parents:
                work.append(d)
        work.sort(key=lambda d: (d.kind != "archive", d.filed_date or datetime.min.replace(tzinfo=UTC)))
        return [d.id for d in work]


def _dt(d) -> datetime | None:
    return datetime(d.year, d.month, d.day, tzinfo=UTC) if d else None


# --------------------------------------------------------------------------- public API


async def _summary(deps: Deps, case_id: uuid.UUID, status: str, newly_embedded: int = 0, **extra) -> dict:
    async with deps.session_factory() as s:
        cov = await coverage(s, case_id)
    return {
        "status": status,
        "total_docs_on_record": cov["total_docs_on_record"],
        "embedded": cov["embedded"],
        "newly_embedded": newly_embedded,
        "skipped": cov["skipped"],
        "blocked": cov["blocked"],
        "failed": [{"title": f["title"], "reason": f["reason"]} for f in cov["failed"]],
        "pending": cov["pending"],
        "complete": cov["complete"],
        "counts": cov["counts"],
        "reasons": cov["reasons"],
        "truncated": cov["truncated"],
        "last_synced_at": cov["last_synced_at"],
        **extra,
    }


async def sync_case_knowledge_base(
    case_id: uuid.UUID,
    *,
    deps: Deps | None = None,
    wait_s: float = 0.0,
    force: bool = False,
) -> dict:
    """Bring `case_id`'s knowledge base in line with its public record.

    Plain async function for now; Phase 4 wraps it as a LangChain tool (the case id will come
    from runtime context, never from the model). Returns the coverage summary described in
    PLAN.md section 5 plus ``complete``, ``counts`` and ``reasons``.
    """
    deps = deps or default_deps()
    async with deps.session_factory() as s:
        if await s.get(Case, case_id) is None:
            raise CaseNotFound(str(case_id))

    lock = CaseLock(deps.engine, case_id)
    if not await lock.acquire(wait_s):
        return await _summary(deps, case_id, "in_progress")
    progress = progress_registry.get(case_id)
    if progress.phase not in ("listing", "ingesting"):
        progress.start()
    try:
        result = await _sync_locked(deps, case_id, progress, force)
        progress.phase, progress.finished_at, progress.result = "done", datetime.now(UTC), result
        return result
    except BaseException as e:
        progress.phase, progress.finished_at = "error", datetime.now(UTC)
        progress.error = type(e).__name__
        raise
    finally:
        await lock.release()


async def _sync_locked(deps: Deps, case_id: uuid.UUID, progress: Progress, force: bool) -> dict:
    s = _s()
    now = deps.clock()
    run = Run(deps=deps, case_id=case_id, now=now, progress=progress)
    async with deps.session_factory() as sess:
        case = await sess.get(Case, case_id)
        rows = list(await sess.scalars(select(Document).where(Document.case_id == case_id)))
    assert case is not None

    # 1. Synced moments ago and nothing to retry: not even a listing request.
    if (
        not force
        and case.last_synced_at
        and rows
        and (now - case.last_synced_at).total_seconds() < s.kb_recheck_min_interval_s
        and not _fast_path_blocker(rows, now)
    ):
        return await _summary(deps, case_id, "up_to_date")

    # 2. List the public record.
    progress.phase = "listing"
    try:
        listing = await records.list_record_documents(case, deps.client)
    except records.RecordListingError as e:
        log.warning("record listing failed: %s", type(e).__name__)
        return await _summary(
            deps, case_id, "listing_failed", error="The public record could not be listed"
        )
    warnings = list(listing.source_errors)
    if listing.dropped_invalid:
        warnings.append(f"{listing.dropped_invalid} listing records were invalid and ignored")

    # 3. Unchanged record and nothing to retry: up to date.
    fp = listing.fingerprint
    if (
        listing.listing_ok
        and case.record_fingerprint == fp
        and rows
        and not _fast_path_blocker(rows, now)
    ):
        async with deps.session_factory() as sess, sess.begin():
            await sess.execute(update(Case).where(Case.id == case_id).values(last_synced_at=now))
        return await _summary(deps, case_id, "up_to_date", warnings=warnings)

    # 4. Make the table match the record, then ingest what needs it.
    work = await _plan(run, listing)
    progress.phase, progress.total, progress.done = "ingesting", len(work), 0
    for doc_id in work:
        async with deps.session_factory() as sess:
            doc = await sess.get(Document, doc_id)
        if doc is None:
            continue
        await _process(run, doc)
        progress.done += 1
    progress.current = None

    values: dict = {"last_synced_at": now}
    if listing.listing_ok:
        values["record_fingerprint"] = fp
    async with deps.session_factory() as sess, sess.begin():
        await sess.execute(update(Case).where(Case.id == case_id).values(**values))
    status = "synced" if (run.newly_embedded or work) else "up_to_date"
    return await _summary(
        deps,
        case_id,
        status,
        newly_embedded=run.newly_embedded,
        warnings=warnings,
        listing_complete=listing.listing_ok,
    )


# --------------------------------------------------------------------------- manual ingest


async def ingest_local_file(
    case_id: uuid.UUID,
    path: Path,
    *,
    title: str | None = None,
    filer: str | None = None,
    for_url: str | None = None,
    deps: Deps | None = None,
) -> dict:
    """Ingest a file the user downloaded by hand (e.g. one the site blocked us from), through
    the same type-check -> parse -> chunk -> embed -> commit path as a download.

    With ``for_url`` it fills the existing (typically ``blocked``) document with that URL;
    otherwise a new document with a ``local-file:`` URL is created."""
    deps = deps or default_deps()
    max_bytes = deps.client.max_document_bytes
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file is larger than the {max_bytes} byte limit")
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    lock = CaseLock(deps.engine, case_id)
    if not await lock.acquire(0):
        raise RuntimeError("a sync of this case is in progress; try again shortly")
    try:
        run = Run(deps=deps, case_id=case_id, now=deps.clock(), progress=Progress())
        async with deps.session_factory() as s, s.begin():
            if await s.get(Case, case_id) is None:
                raise CaseNotFound(str(case_id))
            doc = None
            if for_url:
                doc = await s.scalar(
                    select(Document).where(Document.case_id == case_id, Document.source_url == for_url)
                )
                if doc is None:
                    raise ValueError("no document with that URL on this case")
            if doc is None:
                doc = Document(
                    case_id=case_id,
                    source_url=f"local-file://{sha}",
                    title=(title or path.stem)[:300],
                    filer=filer,
                    kind="local_file",
                    status=DocumentStatus.pending,
                )
                existing = await s.scalar(
                    select(Document).where(Document.source_url == doc.source_url)
                )
                if existing is not None and existing.case_id != case_id:
                    raise ValueError("that file is already attached to a different case")
                doc = existing or doc
                s.add(doc)
            elif title:
                doc.title = title[:300]
            if filer and doc.filer is None:
                doc.filer = filer[:300]
        await _guarded(run, doc, _ingest_bytes, data, claimed=filetype.claimed_type("", None, path.name))
        async with deps.session_factory() as s:
            final = await s.get(Document, doc.id)
        return {
            "status": final.status.value if final else "unknown",
            "reason": final.error if final else None,
            "document_id": str(doc.id),
            "title": final.title if final else None,
            "newly_embedded": run.newly_embedded,
        }
    finally:
        await lock.release()
