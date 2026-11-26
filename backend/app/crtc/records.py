"""Documents on a case's public record.

Sources (discovered in Phase 3, see docs/crtc-documents.md):

1. **Filings** - ``GET applications.crtc.gc.ca/portail-portal/eng/listes-lists/21/data?f=<file>&g=num``
   is the JSON that the "interventions" list page loads with JavaScript. One row per filer
   and document type; the ``dcEn`` field packs ``<DMID>!<file name>|<size>|...``. Every
   file is fetched through ``DocWebBroker/OpenDocument.aspx?DMID=<id>``, which robots.txt
   disallows, so these are *listed* (and shown to the user) but marked blocked at download.
   ``<file>`` is the notice number for consultations (``2026-259``) and the dashed file number
   for applications (``202605387`` -> ``2026-0538-7``).
2. **Related documents** - the links stored on the case row (``cases.document_links``):
   ``TransferToWeb/*.zip`` archives (reachable), notice/letter pages on ``crtc.gc.ca`` and
   ``www.crtc.gc.ca`` (Cloudflare-challenged: blocked), plus the notice itself (``source_url``).

Everything is validated with Pydantic; invalid records are dropped and counted, never fatal.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from langsmith import traceable
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.crtc.client import (
    CrtcClient,
    CrtcClientError,
    FetchBlocked,
    UrlNotAllowed,
    check_url,
)
from app.crtc.html import html_to_text, normalize_text

log = logging.getLogger(__name__)

LISTING_URL = "https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/21/data"
DOCBROKER_URL = "https://applications.crtc.gc.ca/DocWebBroker/OpenDocument.aspx"
MAX_DOCS_PER_RECORD = 5000

RecordKind = Literal["filing", "archive", "notice", "letter", "other"]

DOC_TYPE_LABELS = {
    "INTR": "Intervention",
    "INP2": "Intervention (phase 2)",
    "REPL": "Reply",
    "ANSW": "Answer",
    "FINA": "Final submission",
    "FNRP": "Final reply",
    "CMMT": "Comment",
    "FUCO": "Further comment",
    "UNEG": "Undertaking",
    "PHEA": "Presentation at hearing",
}
_FILE_NO = re.compile(r"^\d{9}$")
_DMID = re.compile(r"^\d{1,12}$")
_TWIN_EXTS = {".html", ".htm"}


class RecordListingError(Exception):
    """The record could not be listed at all (nothing trustworthy to sync against)."""


class RecordDoc(BaseModel):
    """One document on a case's public record, validated and plain-text only."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=500)
    filer: str | None = Field(default=None, max_length=300)
    filed_date: date | None = None
    kind: RecordKind
    doc_type: str | None = Field(default=None, max_length=8)
    variant_of: str | None = Field(default=None, max_length=2048)  # URL of the preferred twin
    signature: str | None = Field(default=None, max_length=300)  # change detector, archives only

    @field_validator("url")
    @classmethod
    def _allowlisted(cls, v: str) -> str:
        try:
            check_url(v)
        except UrlNotAllowed as e:
            raise ValueError(str(e)) from e
        return v


@dataclass
class RecordListing:
    documents: list[RecordDoc] = field(default_factory=list)
    dropped_invalid: int = 0
    dropped_duplicate: int = 0
    source_errors: list[str] = field(default_factory=list)  # human-readable, no document text
    listing_ok: bool = True  # the filings endpoint answered (even if it had zero rows)
    signature_failures: int = 0

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.documents)


def fingerprint(docs: list[RecordDoc]) -> str:
    lines = sorted(
        f"{d.url}|{d.signature or ''}|{d.filed_date or ''}|{d.title}|{d.variant_of or ''}"
        for d in docs
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# --------------------------------------------------------------------------- filings


def listing_key(notice_number: str, document_links: list[dict[str, Any]] | None) -> str:
    """The `f=` value for the filings endpoint."""
    for link in document_links or []:
        u = urlsplit(str(link.get("url", "")))
        if "/listes-lists/interventions/" in u.path:
            for part in u.query.split("&"):
                if part.startswith("f=") and len(part) > 2:
                    return part[2:]
    if _FILE_NO.match(notice_number):  # 202605387 -> 2026-0538-7
        return f"{notice_number[:4]}-{notice_number[4:8]}-{notice_number[8]}"
    return notice_number


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=500)
    cmpny: str = Field(default="", max_length=500)
    date: str = Field(default="", max_length=40)
    typ: str = Field(default="", max_length=8)
    dcEn: str = Field(default="", max_length=200_000)

    @field_validator("name", "cmpny", "date", "typ", "dcEn", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("expected text")  # noqa: TRY004 - pydantic needs ValueError
        return v


def _clean(text: str, max_len: int) -> str:
    return " ".join(normalize_text(text).split())[:max_len]


def parse_filings(payload: Any) -> tuple[list[RecordDoc], int]:
    """Rows of the filings endpoint -> documents. Returns (docs, dropped_invalid)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RecordListingError("filings response has no 'data' list")
    docs: list[RecordDoc] = []
    dropped = 0
    for item in payload["data"]:
        try:
            row = _Row.model_validate(item)
        except ValidationError:
            dropped += 1
            continue
        filer = _clean(row.name, 200)
        if row.cmpny.strip():
            filer = f"{filer} ({_clean(row.cmpny, 95)})" if filer else _clean(row.cmpny, 200)
        try:
            filed = date.fromisoformat(row.date.strip()) if row.date.strip() else None
        except ValueError:
            filed = None
        label = DOC_TYPE_LABELS.get(row.typ, "Filing")
        parts = row.dcEn.split("|")
        row_docs: list[RecordDoc] = []
        for i in range(0, len(parts) - 1, 2):  # pairs: "<id>!<name>", "<size>"
            ident, _, fname = parts[i].partition("!")
            ident, fname = ident.strip(), _clean(fname, 300)
            if not (ident and fname):
                continue  # trailing separator / empty slot, not a bad record
            if not _DMID.match(ident):
                dropped += 1
                continue
            try:
                row_docs.append(
                    RecordDoc(
                        url=f"{DOCBROKER_URL}?DMID={quote(ident)}",
                        title=f"{label}: {fname}"[:500],
                        filer=filer or None,
                        filed_date=filed,
                        kind="filing",
                        doc_type=row.typ or None,
                    )
                )
            except ValidationError:
                dropped += 1
        # PDF + HTML renditions of one filing: keep the PDF as the document, flag the twin.
        by_stem = {
            os.path.splitext(d.title)[0]: d for d in row_docs if d.title.lower().endswith(".pdf")
        }
        for d in row_docs:
            stem, ext = os.path.splitext(d.title)
            if ext.lower() in _TWIN_EXTS and stem in by_stem:
                d = d.model_copy(update={"variant_of": by_stem[stem].url})
            docs.append(d)
    return docs, dropped


# --------------------------------------------------------------------------- related documents


def classify_link(url: str, text: str) -> RecordKind | None:
    """Kind of a stored related-document link, or None if it's a listing page (not a document)."""
    u = urlsplit(url)
    path = u.path.lower()
    if "/listes-lists/" in path:
        return None  # the interventions list pages: covered by the filings endpoint
    if path.endswith(".zip"):
        return "archive"
    base = path.rsplit("/", 1)[-1]
    if "/archive/" in path and (base.startswith(("lt", "lb", "lr")) or "letter" in text.lower()):
        return "letter"
    if path.endswith((".pdf", ".docx", ".doc", ".rtf")):
        return "other"
    return "notice"


def related_documents(
    notice_number: str, source_url: str | None, document_links: list[dict[str, Any]] | None
) -> tuple[list[RecordDoc], int]:
    docs: list[RecordDoc] = []
    dropped = 0
    candidates: list[tuple[str, str, RecordKind | None]] = []
    if source_url:
        # For applications the "source" is the application's own TransferToWeb ZIP.
        kind = classify_link(source_url, "") or "notice"
        candidates.append((source_url, f"Notice {notice_number}", kind))
    for link in document_links or []:
        url, text = str(link.get("url", "")), str(link.get("text", ""))
        candidates.append((url, text, classify_link(url, text)))
    for url, text, kind in candidates:
        if kind is None:
            continue
        title = html_to_text(text, max_len=300) or urlsplit(url).path.rsplit("/", 1)[-1]
        try:
            docs.append(RecordDoc(url=url, title=title or "Document", kind=kind))
        except ValidationError:
            dropped += 1
    return docs, dropped


async def archive_signature(client: CrtcClient, url: str) -> str:
    """Change detector for a ZIP: HEAD (robots-checked, no body) -> size|last-modified|etag."""
    resp = await client.head(url)
    h = resp.headers
    sig = "|".join(h.get(k, "") for k in ("content-length", "last-modified", "etag"))
    if sig == "||":
        raise CrtcClientError("HEAD gave no size/date/etag")
    return sig[:300]


@traceable(
    name="records.list_record_documents",
    run_type="tool",
    process_inputs=lambda i: {"notice": getattr(i.get("case"), "notice_number", None)},
)
async def list_record_documents(
    case: Any, client: CrtcClient, *, sign_archives: bool = True
) -> RecordListing:
    """All documents on `case`'s public record. `case` needs `notice_number`, `source_url`
    and `document_links` (the ORM ``Case`` has them). Never raises for a bad record; raises
    ``RecordListingError`` only if the filings listing fails AND nothing else could be listed."""
    result = RecordListing()
    docs: list[RecordDoc] = []

    key = listing_key(case.notice_number, case.document_links)
    url = f"{LISTING_URL}?f={quote(key, safe='-_.')}&g=num"
    try:
        resp = await client.fetch_listing(url)
        payload = await asyncio.to_thread(_loads, resp.content)
        filings, dropped = parse_filings(payload)
        docs += filings
        result.dropped_invalid += dropped
    except (CrtcClientError, RecordListingError, ValueError) as e:
        result.listing_ok = False
        result.source_errors.append(f"filings listing unavailable: {type(e).__name__}")
        log.warning("filings listing failed for case: %s", type(e).__name__)
        if isinstance(e, FetchBlocked):
            result.source_errors[-1] += f" ({e.reason})"

    related, dropped = related_documents(case.notice_number, case.source_url, case.document_links)
    docs += related
    result.dropped_invalid += dropped

    if not result.listing_ok and not related:
        raise RecordListingError("; ".join(result.source_errors))

    seen: set[str] = set()
    unique: list[RecordDoc] = []
    for d in docs:
        if d.url in seen:
            result.dropped_duplicate += 1
            continue
        seen.add(d.url)
        unique.append(d)
    if len(unique) > MAX_DOCS_PER_RECORD:
        result.dropped_invalid += len(unique) - MAX_DOCS_PER_RECORD
        unique = unique[:MAX_DOCS_PER_RECORD]

    if sign_archives:
        signed: list[RecordDoc] = []
        for d in unique:
            if d.kind == "archive":
                try:
                    d = d.model_copy(update={"signature": await archive_signature(client, d.url)})
                except CrtcClientError:
                    result.signature_failures += 1  # the download step will report the real reason
            signed.append(d)
        unique = signed
    result.documents = unique
    return result


def _loads(raw: bytes) -> Any:
    return json.loads(raw)
