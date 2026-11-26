"""Text blocks -> numbered paragraphs -> paragraph-aware chunks.

Citations depend on this module being right, so the rules are deliberately simple:

Numbering
  * A block that starts with ``N.`` (``^\\d{1,4}\\.\\s``) starts paragraph N *only if N continues
    the sequence* (previous + 1, tolerating a gap of up to 2 for dropped numbers). Anything
    else that merely looks numbered ("2026. The..." mid-text, list items restarting at 1,
    section numbers) is treated as a continuation of the current paragraph.
  * A document is "source"-numbered when at least ``MIN_NUMBERED`` sequence-valid numbered
    paragraphs were found (the sequence must start at 0..2) and they make up >=
    ``MIN_NUMBERED_SHARE`` of all blocks. Otherwise every block becomes paragraph 1..N and numbering is "synthetic".
  * In source-numbered documents, text before the first numbered paragraph is paragraph 0
    (front matter) and unnumbered blocks (headings, page-break continuations) are appended to
    the paragraph they follow. Paragraph numbers are therefore always the document's own.

Chunking
  * Chunks are runs of WHOLE paragraphs, up to ``TARGET_TOKENS``. A single paragraph larger
    than that still becomes one chunk, and is split only when longer than ``OVERSIZE_TOKENS``
    (then at sentence boundaries; every piece keeps the same paragraph number).
  * Every chunk records para_start/para_end (the document's numbers) and the page of its
    first paragraph. In source-numbered documents each paragraph inside ``chunk.text`` starts
    with its own ``N. `` so any paragraph can be recovered exactly; paragraphs are separated
    by a blank line.

Token counts are an estimate (len/4), so no tokenizer has to be loaded to chunk.
"""

import re
from dataclasses import dataclass

from app.crtc.html import normalize_text

TARGET_TOKENS = 350
OVERSIZE_TOKENS = 800
MIN_NUMBERED = 3
MIN_NUMBERED_SHARE = 0.3
MAX_NUMBER_GAP = 2

_NUM_START = re.compile(r"^\s*(\d{1,4})\.\s+(\S.*)$", re.DOTALL)
_NUM_ALONE = re.compile(r"^\s*(\d{1,4})\.\s*$")
_PAGE_NOISE = re.compile(r"^\s*(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")


@dataclass(frozen=True)
class Block:
    """A run of text as the parser found it (a PDF text block, an HTML <p>, a DOCX paragraph)."""

    text: str
    page: int | None = None


@dataclass(frozen=True)
class Paragraph:
    number: int
    text: str  # no leading number
    page: int | None


@dataclass(frozen=True)
class ChunkOut:
    para_start: int
    para_end: int
    page: int | None
    text: str


def estimate_tokens(text: str) -> int:
    return max(1, -(-len(text) // 4))


def clean_text(text: str) -> str:
    """NFC, drop control/zero-width/bidi characters, collapse all whitespace to single spaces."""
    return " ".join(normalize_text(text).split())


def _clean_blocks(blocks: list[Block]) -> list[Block]:
    out: list[Block] = []
    pending_number: str | None = None
    for b in blocks:
        t = clean_text(b.text)
        if not t:
            continue
        if _NUM_ALONE.match(t):  # "12." alone in its own block: the number of the next block
            pending_number = t.rstrip()
            continue
        if _PAGE_NOISE.match(t):  # bare page numbers / "Page 3 of 10"
            continue
        if pending_number:
            t, pending_number = f"{pending_number} {t}", None
        out.append(Block(t, b.page))
    return out


def _accept(n: int, last: int | None) -> bool:
    if last is None:
        return n <= 2
    return last < n <= last + 1 + MAX_NUMBER_GAP


def build_paragraphs(blocks: list[Block]) -> tuple[list[Paragraph], str]:
    """Return (paragraphs, numbering) where numbering is "source" or "synthetic"."""
    cleaned = _clean_blocks(blocks)
    if not cleaned:
        return [], "synthetic"

    # Pass 1: how many blocks start a sequence-valid numbered paragraph?
    last: int | None = None
    accepted = 0
    for b in cleaned:
        m = _NUM_START.match(b.text)
        if m and _accept(int(m.group(1)), last):
            last, accepted = int(m.group(1)), accepted + 1
    numbered = accepted >= MIN_NUMBERED and accepted / len(cleaned) >= MIN_NUMBERED_SHARE

    if not numbered:
        return [Paragraph(i, b.text, b.page) for i, b in enumerate(cleaned, 1)], "synthetic"

    paras: list[Paragraph] = []
    cur_n: int | None = None
    cur_parts: list[str] = []
    cur_page: int | None = None
    last = None

    def flush() -> None:
        if cur_parts:
            paras.append(Paragraph(cur_n if cur_n is not None else 0, " ".join(cur_parts), cur_page))

    for b in cleaned:
        m = _NUM_START.match(b.text)
        if m and _accept(int(m.group(1)), last):
            flush()
            cur_n, last = int(m.group(1)), int(m.group(1))
            cur_parts, cur_page = [m.group(2).strip()], b.page
        else:
            if not cur_parts:  # front matter before the first numbered paragraph
                cur_n, cur_page = 0, b.page
            cur_parts.append(b.text)
    flush()
    return paras, "source"


def _split_oversize(text: str) -> list[str]:
    """Split one very long paragraph at sentence boundaries (words as a last resort)."""
    pieces: list[str] = []
    cur: list[str] = []
    cur_tok = 0

    def push() -> None:
        nonlocal cur, cur_tok
        if cur:
            pieces.append(" ".join(cur))
        cur, cur_tok = [], 0

    for sentence in _SENTENCE_END.split(text):
        units = [sentence]
        if estimate_tokens(sentence) > TARGET_TOKENS:  # one enormous "sentence": split on words
            words = sentence.split()
            step = TARGET_TOKENS * 4 // 6  # ~ words that fit the target
            units = [" ".join(words[i : i + step]) for i in range(0, len(words), step)]
        for unit in units:
            t = estimate_tokens(unit)
            if cur and cur_tok + t > TARGET_TOKENS:
                push()
            cur.append(unit)
            cur_tok += t
    push()
    return pieces


def chunk_paragraphs(paragraphs: list[Paragraph], numbering: str) -> list[ChunkOut]:
    chunks: list[ChunkOut] = []
    cur: list[Paragraph] = []
    cur_tok = 0

    def render(p: Paragraph, text: str | None = None) -> str:
        body = text if text is not None else p.text
        return f"{p.number}. {body}" if numbering == "source" else body

    def flush() -> None:
        nonlocal cur, cur_tok
        if cur:
            chunks.append(
                ChunkOut(
                    cur[0].number,
                    cur[-1].number,
                    cur[0].page,
                    "\n\n".join(render(p) for p in cur),
                )
            )
        cur, cur_tok = [], 0

    for p in paragraphs:
        t = estimate_tokens(p.text)
        if t > OVERSIZE_TOKENS:
            flush()
            for piece in _split_oversize(p.text):
                chunks.append(ChunkOut(p.number, p.number, p.page, render(p, piece)))
            continue
        if cur and cur_tok + t > TARGET_TOKENS:
            flush()
        cur.append(p)
        cur_tok += t
    flush()
    return chunks
