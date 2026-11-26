"""Test doubles shared by the Phase 3 tests."""

import hashlib
import math
import struct

from app.config import get_settings


class HashEmbedder:
    """Deterministic fake embedder: same text -> same unit vector. Never loads a model."""

    def __init__(self, dim: int | None = None, fail_on: str | None = None) -> None:
        self.dim = dim or get_settings().embedding_dim
        self.model_name = "hash-embedder"
        self.calls = 0  # number of embed_passages calls
        self.texts_embedded = 0  # total texts embedded (what "zero embeddings" asserts on)
        self.fail_on = fail_on  # raise if any text contains this marker

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_on and any(self.fail_on in t for t in texts):
            raise RuntimeError("embedder exploded")
        self.texts_embedded += len(texts)
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def _vec(self, text: str) -> list[float]:
        raw = b""
        counter = 0
        while len(raw) < self.dim * 4:
            raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
            counter += 1
        nums = [struct.unpack_from(">i", raw, i * 4)[0] / 2**31 for i in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in nums)) or 1.0
        return [x / norm for x in nums]


# --------------------------------------------------------------------------- document builders

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pymupdf

FIXTURES = Path(__file__).parent / "fixtures"


def make_pdf(paragraphs: list[str], *, per_page: int = 12) -> bytes:
    """A real PDF with each paragraph well separated, so MuPDF reports one block each."""
    doc = pymupdf.open()
    page = None
    for i, text in enumerate(paragraphs):
        if i % per_page == 0:
            page = doc.new_page()
        y = 60 + (i % per_page) * 55
        page.insert_text((50, y), text, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out


def make_blank_pdf(pages: int = 2) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    return doc.tobytes()


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 60), "secret " * 20, fontsize=10)
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")


def make_docx(paragraphs: list[str], extra_xml: str = "") -> bytes:
    from xml.sax.saxutils import escape

    body = "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>' + extra_xml
        + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def make_html(paragraphs: list[str], extra: str = "") -> bytes:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<!DOCTYPE html><html><head><title>t</title></head><body>{body}{extra}</body></html>".encode()


def make_zip(members: dict[str, bytes], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- fake CRTC site

APPS = "https://applications.crtc.gc.ca"
LISTING = f"{APPS}/portail-portal/eng/listes-lists/21/data"


class FakeSite:
    """An in-memory CRTC. `site.log` records every request as (METHOD, url)."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.log: list[tuple[str, str]] = []
        robots = (FIXTURES / "robots_applications.txt").read_text()
        self.add_text(f"{APPS}/robots.txt", robots)
        open_robots = "User-agent: *\nAllow: /\n"
        self.add_text("https://crtc.gc.ca/robots.txt", open_robots)
        self.add_text("https://www.crtc.gc.ca/robots.txt", open_robots)

    def add(self, url: str, content: bytes = b"", status: int = 200, headers=None) -> None:
        hdrs = dict(headers or {})
        self.routes[url] = lambda req: httpx.Response(status, content=content, headers=hdrs)

    def add_text(self, url: str, text: str) -> None:
        self.add(url, text.encode(), headers={"content-type": "text/plain"})

    def add_json(self, url: str, obj) -> None:
        self.add(url, json.dumps(obj).encode(), headers={"content-type": "application/json"})

    def add_zip(self, url: str, data: bytes, last_modified: str = "Mon, 01 Jan 2026 00:00:00 GMT"):
        self.add(
            url,
            data,
            headers={
                "content-type": "application/x-zip-compressed",
                "last-modified": last_modified,
                "etag": f'"{len(data)}-{last_modified}"',
            },
        )

    def add_cloudflare_block(self, url: str) -> None:
        self.add(url, b"<html>Security Check</html>", 403, {"cf-mitigated": "challenge"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.log.append((request.method, url))
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, content=b"not found")
        resp = route(request)
        if request.method == "HEAD":
            return httpx.Response(resp.status_code, headers={**resp.headers, "content-length": str(len(resp.content))})
        return resp

    def requests(self, method: str | None = None, contains: str = "") -> list[str]:
        return [u for m, u in self.log if (method is None or m == method) and contains in u]

    def document_gets(self) -> list[str]:
        """GETs that are neither robots.txt nor the filings listing."""
        return [
            u for m, u in self.log
            if m == "GET" and not u.endswith("/robots.txt") and "/listes-lists/21/data" not in u
        ]


def filings_payload(rows: list[dict]) -> dict:
    return {"data": rows}


def filing_row(name="Doe, Jane", typ="INTR", docs=((1000001, "Intervention.PDF", "10 KB"),), date="2026-11-16", cmpny=""):
    dc = "".join(f"{i}!{n}|{size}|" for i, n, size in docs)
    return {
        "apl": "", "name": name, "cmpny": cmpny, "date": date, "itm": "1", "num": "x",
        "typ": typ, "sbjEn": "Subject", "sbjFr": "Sujet", "dcEn": dc, "dcFr": dc, "evnt": "n", "cnum": "1",
    }


# --------------------------------------------------------------------------- fast in-process parse


async def fast_parse(path, kind):
    """Same contract as app.ingest.parse.parse_file_async, but PDFs are read in-process (fast).
    HTML/DOCX (and every failure mode that matters) still go through the real subprocess."""
    from app.ingest.chunk import Block
    from app.ingest.parse import ParsedDocument, ParseFailed, parse_file_async

    if kind != "pdf":
        return await parse_file_async(path, kind)
    doc = pymupdf.open(stream=Path(path).read_bytes(), filetype="pdf")
    blocks, empty, total = [], 0, 0
    for i, page in enumerate(doc):
        got = [b for b in page.get_text("blocks", sort=True) if b[6] == 0 and b[4].strip()]
        blocks += [Block(b[4], i + 1) for b in got]
        total += sum(len(b[4].strip()) for b in got)
        empty += not got
    if total < 40:
        raise ParseFailed("needs_ocr")
    return ParsedDocument(blocks=blocks, pages=doc.page_count, pages_without_text=empty)
