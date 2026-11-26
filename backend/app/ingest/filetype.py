"""Detect a file's real type from its bytes (never from its extension or Content-Type).

Allowlist: PDF, DOCX, HTML. Everything else is ``skipped`` by the caller with a reason.
A claimed type (from the URL extension or Content-Type) that disagrees with the detected type
is a ``type_mismatch``: typical real-world cause is a challenge/error page served with a 200
in place of the document, so it must never be parsed as if it were the document.
"""

import io
import zipfile
from dataclasses import dataclass
from urllib.parse import urlsplit

PDF = "pdf"
DOCX = "docx"
HTML = "html"
ZIP = "zip"  # an archive: handled by ingest.archive, never parsed as text
OLE = "ole"  # legacy .doc/.xls/.ppt
RTF = "rtf"
UNKNOWN = "unknown"
WORD = "word"  # a claim only (".doc"/msword): satisfied by a detected DOCX or legacy OLE file

PARSEABLE = {PDF, DOCX, HTML}

_HTML_STARTS = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<meta",
    b"<title",
    b"<div",
    b"<p>",
    b"<p ",
    b"<h1",
    b"<table",
    b"<?xml",  # XHTML
)
_EXTENSIONS = {
    ".pdf": PDF,
    ".docx": DOCX,
    ".htm": HTML,
    ".html": HTML,
    ".zip": ZIP,
    ".doc": WORD,  # "a Word document": 14 of 17 real CRTC ".doc" files are DOCX in disguise
    ".rtf": RTF,
}
_CONTENT_TYPES = {
    "application/pdf": PDF,
    "application/x-pdf": PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCX,
    "text/html": HTML,
    "application/xhtml+xml": HTML,
    "application/zip": ZIP,
    "application/x-zip-compressed": ZIP,
    "application/msword": WORD,
    "application/rtf": RTF,
    "text/rtf": RTF,
}
MAX_PROBE = 8192


@dataclass(frozen=True)
class Detected:
    type: str
    mime: str


_MIME = {
    PDF: "application/pdf",
    DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    HTML: "text/html",
    ZIP: "application/zip",
    OLE: "application/x-ole-storage",
    RTF: "application/rtf",
    UNKNOWN: "application/octet-stream",
}


def detect_type(data: bytes) -> Detected:
    """Sniff `data`. For ZIP containers, DOCX is recognised by its mandatory parts."""
    head = data[:MAX_PROBE]
    if b"%PDF-" in head[:1024]:  # the spec allows a little junk before the header
        return Detected(PDF, _MIME[PDF])
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        kind = DOCX if _is_docx(data) else ZIP
        return Detected(kind, _MIME[kind])
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return Detected(OLE, _MIME[OLE])
    if head.startswith(b"{\\rtf"):
        return Detected(RTF, _MIME[RTF])
    if _looks_like_html(head):
        return Detected(HTML, _MIME[HTML])
    return Detected(UNKNOWN, _MIME[UNKNOWN])


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = {i.filename for i in z.infolist()[:2000]}
    except (zipfile.BadZipFile, ValueError, OSError):
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


def _looks_like_html(head: bytes) -> bool:
    if b"\x00" in head:  # binary, or UTF-16 (not supported: skipped as unknown)
        return False
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n\x0c").lower()
    if text.startswith(_HTML_STARTS):
        return True
    # Plain-text preambles (comments, conditional comments) before the real markup.
    return text.startswith(b"<!--") and any(t in text for t in (b"<html", b"<body", b"<head"))


def claimed_type(url: str, content_type: str | None, name: str | None = None) -> str | None:
    """What the server/URL SAYS this is (family name), or None if it makes no useful claim."""
    claims: set[str] = set()
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct in _CONTENT_TYPES:
            claims.add(_CONTENT_TYPES[ct])
    for candidate in (name, urlsplit(url).path):
        if candidate:
            ext = "." + candidate.rsplit(".", 1)[-1].lower() if "." in candidate else ""
            if ext in _EXTENSIONS:
                claims.add(_EXTENSIONS[ext])
    if len(claims) == 1:
        return next(iter(claims))
    if len(claims) > 1:
        return "conflict"  # e.g. header says PDF but the URL says .htm: never trust either
    return None


def check_type(detected: Detected, claimed: str | None) -> str | None:
    """Return a mismatch description, or None if the claim is absent/consistent.

    A DOCX is a ZIP container, so a "zip" claim for a detected DOCX (and vice versa) is fine.
    """
    if claimed is None:
        return None
    if claimed == "conflict":
        return "type_mismatch: conflicting type claims"
    if claimed == detected.type:
        return None
    if {claimed, detected.type} <= {ZIP, DOCX}:
        return None
    if claimed == WORD and detected.type in (DOCX, OLE):
        return None
    return f"type_mismatch: claimed {claimed}, detected {detected.type}"
