"""Out-of-process text extractor. Run as ``python -I parse_worker.py <pdf|docx|html> <path>``.

SECURITY: this script is the only code that ever touches the bytes of an untrusted document,
so it is kept small, self-contained (stdlib + pymupdf + defusedxml; it imports nothing from
``app``) and it runs in its own interpreter in isolated mode (``-I``), with a wall-clock
timeout and a memory limit imposed by the parent (a Job object on Windows, RLIMIT_AS here on
POSIX). It prints ONE JSON document on stdout and nothing else.

It extracts TEXT ONLY:
  * PDF  - text blocks via MuPDF. No JavaScript is run, no embedded files are opened,
           no annotations/forms are processed, nothing is rendered.
  * DOCX - ``word/document.xml`` only, parsed with defusedxml (DTDs/entities refused). Macros,
           embedded objects, headers/footers and external relationships are never opened.
  * HTML - the stdlib tokenizer; scripts, styles, iframes, objects, embeds, templates and
           their contents are dropped; tags are stripped; nothing is fetched or rendered.

The parent re-validates and normalises everything this prints (the worker may be compromised).
"""

import json
import os
import sys
import zipfile
from html.parser import HTMLParser

MAX_PAGES = 1500
MAX_CHARS = 8_000_000
MAX_XML_BYTES = 100 * 1024 * 1024
MAX_XML_RATIO = 200
MIN_TEXT_CHARS = 40  # a "PDF" with less text than this in total has no usable text layer


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=True))
    sys.stdout.flush()


def fail(code: str) -> None:
    emit({"ok": False, "error": code})
    sys.exit(0)


def limit_memory_posix() -> None:
    mb = int(os.environ.get("KB_PARSE_MEMORY_MB", "0") or 0)
    if not mb:
        return
    try:
        import resource  # POSIX only

        limit = mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass  # Windows: the parent's Job object enforces the limit


class Budget:
    def __init__(self) -> None:
        self.chars = 0

    def add(self, text: str) -> None:
        self.chars += len(text)
        if self.chars > MAX_CHARS:
            fail("too_much_text")


# ----------------------------------------------------------------------------- PDF


def parse_pdf(path: str) -> dict:
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception:  # noqa: BLE001 - hostile input: any failure means "malformed"
        fail("malformed")
    try:
        if doc.needs_pass or doc.is_encrypted:
            fail("encrypted")
        n = doc.page_count
        if n > MAX_PAGES:
            fail("too_many_pages")
        budget = Budget()
        blocks: list[dict] = []
        empty_pages = 0
        total = 0
        for i in range(n):
            try:
                page_blocks = doc[i].get_text("blocks", sort=True)
            except Exception:  # noqa: BLE001 - hostile input: any failure means "malformed"
                fail("malformed")
            page_chars = 0
            for b in page_blocks:
                if len(b) >= 7 and b[6] == 0:  # type 0 = text (1 = image)
                    text = str(b[4])
                    if text.strip():
                        budget.add(text)
                        page_chars += len(text.strip())
                        blocks.append({"page": i + 1, "text": text})
            total += page_chars
            if page_chars == 0:
                empty_pages += 1
        if n == 0 or total < MIN_TEXT_CHARS:
            fail("needs_ocr")
        return {"ok": True, "kind": "pdf", "pages": n, "pages_without_text": empty_pages,
                "blocks": blocks}
    finally:
        doc.close()


# ----------------------------------------------------------------------------- DOCX

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parse_docx(path: str) -> dict:
    from defusedxml import ElementTree as SafeET
    from defusedxml.common import DefusedXmlException

    try:
        with zipfile.ZipFile(path) as z:
            try:
                info = z.getinfo("word/document.xml")
            except KeyError:
                fail("malformed")
            if info.file_size > MAX_XML_BYTES or (
                info.file_size > 65536 and info.file_size / max(info.compress_size, 1) > MAX_XML_RATIO
            ):
                fail("too_large")
            with z.open(info) as f:
                data = f.read(MAX_XML_BYTES + 1)
            if len(data) > MAX_XML_BYTES:
                fail("too_large")
    except (zipfile.BadZipFile, OSError, ValueError):
        fail("malformed")
    try:
        root = SafeET.fromstring(data, forbid_dtd=True)
    except (DefusedXmlException, SafeET.ParseError, ValueError):
        fail("malformed")

    budget = Budget()
    blocks: list[dict] = []
    for p in root.iter(W + "p"):
        parts: list[str] = []
        for el in p.iter():
            if el.tag == W + "t" and el.text:
                parts.append(el.text)
            elif el.tag in (W + "tab", W + "br", W + "cr"):
                parts.append(" ")
            elif el.tag == W + "noBreakHyphen":
                parts.append("-")
        text = "".join(parts).strip()
        if text:
            budget.add(text)
            blocks.append({"page": None, "text": text})
    if not blocks:
        fail("empty")
    return {"ok": True, "kind": "docx", "pages": None, "pages_without_text": 0, "blocks": blocks}


# ----------------------------------------------------------------------------- HTML

_SKIP = {"script", "style", "template", "noscript", "iframe", "object", "embed", "svg",
         "head", "title", "canvas", "audio", "video", "applet"}
_BLOCK = {"p", "div", "li", "tr", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6",
          "blockquote", "section", "article", "header", "footer", "pre", "br", "dd", "dt",
          "caption", "figcaption", "hr", "form", "fieldset"}
_CELL = {"td", "th"}


class _Extract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.cur: list[str] = []
        self.skip = 0
        self.budget = Budget()

    def _end_block(self) -> None:
        text = " ".join("".join(self.cur).split())
        self.cur = []
        if text:
            self.budget.add(text)
            self.blocks.append(text)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self._end_block()
        elif tag in _CELL:
            self.cur.append(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK:
            self._end_block()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self._end_block()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.cur.append(data)


def decode_html(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")  # CRTC's older pages are windows-1252


def parse_html(path: str) -> dict:
    with open(path, "rb") as f:
        text = decode_html(f.read())
    parser = _Extract()
    parser.feed(text)
    parser.close()
    parser._end_block()
    if not parser.blocks:
        fail("empty")
    return {"ok": True, "kind": "html", "pages": None, "pages_without_text": 0,
            "blocks": [{"page": None, "text": t} for t in parser.blocks]}


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("pdf", "docx", "html"):
        fail("usage")
    limit_memory_posix()
    kind, path = sys.argv[1], sys.argv[2]
    try:
        result = {"pdf": parse_pdf, "docx": parse_docx, "html": parse_html}[kind](path)
    except SystemExit:
        raise
    except MemoryError:
        fail("out_of_memory")
    except RecursionError:
        fail("malformed")
    except Exception:  # noqa: BLE001 - hostile input: any failure means "malformed"
        fail("malformed")
    emit(result)


if __name__ == "__main__":
    main()
