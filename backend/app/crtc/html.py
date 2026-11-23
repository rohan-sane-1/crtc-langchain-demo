"""Untrusted-HTML helpers: plain-text extraction and safe link extraction.

Uses the stdlib HTML tokenizer (no regex on markup). Nothing here ever returns markup.
"""

import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from app.crtc.client import ALLOWED_DOMAIN, is_allowed_host

MAX_URL_LEN = 2048
MAX_LINKS_PER_FIELD = 50
MAX_LINK_TEXT_LEN = 200

_BLOCK_TAGS = {"br", "p", "div", "li", "tr", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5"}
_SKIP_CONTENT_TAGS = {"script", "style", "template", "noscript"}
# URL characters we leave alone when percent-encoding (spaces etc. get encoded).
_URL_SAFE = "/%:@!$&'()*+,;=-._~?#[]"


def normalize_text(text: str) -> str:
    """NFC-normalise; drop control, format (zero-width, bidi), surrogate and private-use chars;
    turn every kind of whitespace into a plain space (newlines are kept as '\\n')."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", text):
        if ch == "\n":
            out.append("\n")
            continue
        cat = unicodedata.category(ch)
        if cat in ("Zs", "Zl", "Zp") or ch in "\t\r\x0b\x0c":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return "".join(out)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str, *, max_len: int, sep: str = " | ") -> str:
    """Strip all markup, normalise, join non-empty lines with `sep`, truncate to `max_len`."""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    text = normalize_text("".join(parser.parts))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    joined = sep.join(line for line in lines if line)
    if len(joined) > max_len:
        joined = joined[: max_len - 1].rstrip() + "…"
    return joined


@dataclass(frozen=True)
class Link:
    url: str
    text: str


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str | None, str]] = []  # (href, text)
        self._href: str | None = None
        self._buf: list[str] = []
        self._in_a = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            self._flush()
            self._in_a = True
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._buf.append(data)

    def _flush(self) -> None:
        if self._in_a:
            self.found.append((self._href, "".join(self._buf)))
        self._in_a = False
        self._href = None
        self._buf = []

    def close(self) -> None:
        super().close()
        self._flush()


def safe_url(href: str, base_url: str) -> str | None:
    """Resolve `href` against `base_url`; return it only if it is a clean https URL on the
    CRTC allowlist. Everything else (javascript:, data:, off-host, malformed) -> None.
    `http://` links on allowlisted hosts are upgraded to `https://` (same host and path)."""
    return _resolve(href, base_url)[0]


def _resolve(href: str, base_url: str) -> tuple[str | None, bool]:
    """Returns (url or None, was_upgraded_from_http)."""
    # Browsers strip leading/trailing whitespace and embedded tab/CR/LF from URLs.
    href = "".join(ch for ch in href.strip() if ch not in "\t\r\n")
    href = normalize_text(href)
    if not href or len(href) > MAX_URL_LEN:
        return None, False
    head = href.split("/", 1)[0]
    if ":" not in head and not href.startswith(("/", ".", "#", "?")) and "." in head:
        # Scheme-less host-looking value such as "www.crtc.gc.ca/x": a relative resolve would
        # silently produce a wrong (but allowlisted) URL. Reject instead.
        return None, False
    try:
        parts = urlsplit(urljoin(base_url, href))
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None, False
    upgraded = parts.scheme == "http"
    if parts.scheme not in ("https", "http") or not host or not is_allowed_host(host):
        return None, False
    # The allowlist check above runs BEFORE the upgrade, so upgrading never widens it.
    if parts.username is not None or parts.password is not None:
        return None, False
    if port not in (None, 80 if upgraded else 443):
        return None, False
    if " " in host or not host.isascii():
        return None, False
    url = urlunsplit(
        (
            "https",
            host.lower().rstrip("."),
            quote(parts.path, safe=_URL_SAFE),
            quote(parts.query, safe=_URL_SAFE),
            "",
        )
    )
    return (url, upgraded) if len(url) <= MAX_URL_LEN else (None, False)


def extract_links(html: str, base_url: str) -> tuple[list[Link], int, int]:
    """Return (safe links in document order, deduplicated; number of rejected hrefs;
    number of links recovered by upgrading http -> https)."""
    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    links: list[Link] = []
    seen: set[str] = set()
    rejected = upgraded = 0
    for href, text in parser.found:
        if href is None:
            continue
        url, was_upgraded = _resolve(href, base_url)
        if url is None:
            rejected += 1
            continue
        upgraded += was_upgraded
        if url in seen:
            continue
        seen.add(url)
        if len(links) < MAX_LINKS_PER_FIELD:
            links.append(Link(url, html_to_text(text, max_len=MAX_LINK_TEXT_LEN)))
    return links, rejected, upgraded


__all__ = ["ALLOWED_DOMAIN", "Link", "extract_links", "html_to_text", "normalize_text", "safe_url"]
