"""Hardened HTTP client for crtc.gc.ca.

Security properties (each is covered by a test in tests/test_crtc_client.py and
tests/test_client_documents.py):

* HTTPS only, TLS verification always on (``verify=True`` is hard-coded; there is no switch).
* Host allowlist (``crtc.gc.ca`` and ``*.crtc.gc.ca``) checked on the first URL and on EVERY
  redirect hop. Redirects are followed manually (httpx ``follow_redirects=False``) and capped.
* SSRF guard: every hostname is resolved and each address must be globally routable. Private,
  loopback, link-local, multicast, reserved and unspecified addresses are refused.
  Residual risk: httpx re-resolves the name when it connects (DNS rebinding window). It is
  mitigated, not eliminated, by TLS verification: the peer must present a valid certificate
  for an allowlisted ``*.crtc.gc.ca`` name, which an internal service will not have.
* The size cap is enforced while streaming; the body is discarded as soon as it is exceeded.
* Connect/read timeouts, bounded retries with exponential backoff, and a minimum interval
  between requests. A descriptive User-Agent is sent.
* Logs contain only URLs (without query strings), status codes and byte counts, never headers,
  cookies or bodies.

Phase 3 additions (document downloads):

* robots.txt is fetched once per host (cached) and obeyed on every hop when ``check_robots`` is
  on, which it always is for ``fetch_document`` / ``fetch_listing`` / ``head``. A path that
  robots disallows is never requested: ``RobotsDisallowed`` is raised before any network call.
* The client never disguises itself: one honest User-Agent, no header tricks, no proxies, no
  attempt to solve a challenge. HTTP 401/403/451, 429 and Cloudflare challenges raise
  ``FetchBlocked`` immediately with NO retry, so callers can record the document as blocked.
"""

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings
from app.crtc.robots import MAX_ROBOTS_BYTES, RobotsRules, parse_robots

log = logging.getLogger(__name__)

# httpx logs every request URL (with query string) at INFO. Keep it out of our logs; we log
# a sanitised line ourselves.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

ALLOWED_DOMAIN = "crtc.gc.ca"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RETRY_STATUSES = {429, 500, 502, 503, 504}
BLOCK_STATUSES = {401, 403, 451}
MAX_RETRY_AFTER_S = 30.0
ROBOTS_TTL_S = 3600.0
ROBOTS_ERROR_TTL_S = 300.0

Resolver = Callable[[str], Awaitable[list[str]]]


class CrtcClientError(Exception):
    """Base class for all client failures."""


class UrlNotAllowed(CrtcClientError):
    """Scheme, host, port or resolved address is not permitted."""


class TooManyRedirects(CrtcClientError):
    pass


class ResponseTooLarge(CrtcClientError):
    pass


class FetchFailed(CrtcClientError):
    """Network error or bad status after retries were exhausted."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FetchBlocked(FetchFailed):
    """The server (or robots.txt) refuses us. Never retried. `reason` is a short stable code."""

    def __init__(self, reason: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message, status_code)
        self.reason = reason


class RobotsDisallowed(FetchBlocked):
    def __init__(self, url: str, rule: str | None) -> None:
        super().__init__(
            "robots_disallowed",
            f"robots.txt disallows {_safe_url(url)}" + (f" (rule {rule})" if rule else ""),
        )
        self.rule = rule


def is_allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN)


def check_url(url: str) -> str:
    """Raise UrlNotAllowed unless `url` is https on an allowlisted host. Returns the hostname."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as e:
        raise UrlNotAllowed("malformed URL") from e
    if parts.scheme != "https":
        raise UrlNotAllowed(f"scheme {parts.scheme!r} not allowed (https only)")
    if not host or not is_allowed_host(host):
        raise UrlNotAllowed(f"host {host!r} is not on the allowlist")
    if parts.username is not None or parts.password is not None:
        raise UrlNotAllowed("credentials in URL not allowed")
    if port not in (None, 443):
        raise UrlNotAllowed(f"port {port} not allowed")
    return host


def _check_ip(addr: str) -> None:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])  # drop IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise UrlNotAllowed(f"host resolves to non-public address {ip}")


async def default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _safe_url(url: str) -> str:
    """URL without query/fragment, for logging."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}{p.path}"


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL after redirects
    status_code: int
    content: bytes
    headers: httpx.Headers

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")


class CrtcClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_response_bytes: int | None = None,
        max_document_bytes: int | None = None,
        max_redirects: int | None = None,
        max_retries: int | None = None,
        min_interval_s: float | None = None,
        connect_timeout_s: float | None = None,
        read_timeout_s: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        s = get_settings()
        self.max_response_bytes = max_response_bytes or s.crtc_max_response_bytes
        self.max_document_bytes = max_document_bytes or s.crtc_max_document_bytes
        self.max_redirects = s.crtc_max_redirects if max_redirects is None else max_redirects
        self.max_retries = s.crtc_max_retries if max_retries is None else max_retries
        self.min_interval_s = (
            s.crtc_min_request_interval_s if min_interval_s is None else min_interval_s
        )
        self._timeout = httpx.Timeout(
            connect=connect_timeout_s or s.crtc_connect_timeout_s,
            read=read_timeout_s or s.crtc_read_timeout_s,
            write=10.0,
            pool=10.0,
        )
        self._user_agent = user_agent or s.crtc_user_agent
        self._transport = transport
        self._resolver = resolver
        self._sleep = sleep
        self._clock = clock
        self._next_slot = 0.0
        self._robots: dict[str, tuple[RobotsRules, float]] = {}

    @property
    def product_token(self) -> str:
        """Our robots.txt product token: the part of the User-Agent before the first '/'."""
        return self._user_agent.split("/", 1)[0].split()[0]

    def _new_http_client(self) -> httpx.AsyncClient:
        # verify=True is deliberate and not configurable. follow_redirects=False because we
        # validate every hop ourselves. trust_env=False so proxy/CA env vars can't weaken this.
        return httpx.AsyncClient(
            transport=self._transport,
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=self._timeout,
            headers={"User-Agent": self._user_agent, "Accept": "application/json, */*;q=0.5"},
        )

    async def _rate_limit(self) -> None:
        # No await between read and write of _next_slot, so concurrent callers get distinct slots.
        now = self._clock()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self.min_interval_s
        if slot > now:
            await self._sleep(slot - now)

    async def _validate(self, url: str) -> None:
        host = check_url(url)
        try:
            addrs = await self._resolver(host)
        except OSError as e:
            raise FetchFailed(f"DNS resolution failed for {host}") from e
        if not addrs:
            raise FetchFailed(f"DNS returned no addresses for {host}")
        for addr in addrs:  # every address must be public, not just the first
            _check_ip(addr)

    # ------------------------------------------------------------------ public API

    async def fetch(self, url: str, *, check_robots: bool = False) -> FetchResult:
        """GET `url`, following validated redirects. Raises CrtcClientError subclasses."""
        return await self._request("GET", url, self.max_response_bytes, check_robots, False)

    async def fetch_listing(self, url: str) -> FetchResult:
        """GET a small JSON/HTML listing: robots.txt obeyed; 403/429 raise FetchBlocked."""
        return await self._request("GET", url, self.max_response_bytes, True, True)

    async def fetch_document(self, url: str) -> FetchResult:
        """GET a document: robots.txt obeyed, size capped at `max_document_bytes` while
        streaming, 401/403/429/451 and Cloudflare challenges raise FetchBlocked at once."""
        return await self._request("GET", url, self.max_document_bytes, True, True)

    async def head(self, url: str) -> FetchResult:
        """HEAD `url` (robots.txt obeyed). The result has no body."""
        return await self._request("HEAD", url, 0, True, True)

    async def _request(
        self, method: str, url: str, max_bytes: int, check_robots: bool, blocked_mode: bool
    ) -> FetchResult:
        async with self._new_http_client() as http:
            current = url
            for _hop in range(self.max_redirects + 1):
                await self._validate(current)
                if check_robots:
                    await self._enforce_robots(http, current)
                resp = await self._get_with_retries(http, method, current, max_bytes, blocked_mode)
                if resp.status_code in REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchFailed("redirect without Location header", resp.status_code)
                    nxt = urljoin(current, location)
                    log.info("redirect %s -> %s", _safe_url(current), _safe_url(nxt))
                    current = nxt  # validated at top of the next iteration
                    continue
                if not 200 <= resp.status_code < 300:
                    raise FetchFailed(f"unexpected HTTP {resp.status_code}", resp.status_code)
                return resp
            raise TooManyRedirects(f"more than {self.max_redirects} redirects")

    # ------------------------------------------------------------------ robots.txt

    async def _enforce_robots(self, http: httpx.AsyncClient, url: str) -> None:
        parts = urlsplit(url)
        rules = await self._rules_for(http, parts.hostname or "")
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        if not rules.is_allowed(target):
            raise RobotsDisallowed(url, rules.matching_rule(target))

    async def _rules_for(self, http: httpx.AsyncClient, host: str) -> RobotsRules:
        host = host.lower()
        cached = self._robots.get(host)
        if cached and cached[1] > self._clock():
            return cached[0]
        robots_url = f"https://{host}/robots.txt"
        await self._validate(robots_url)
        rules, ttl = RobotsRules(), ROBOTS_TTL_S
        try:
            resp = await self._get_with_retries(http, "GET", robots_url, MAX_ROBOTS_BYTES, False)
        except FetchFailed as e:
            if e.status_code is not None and 400 <= e.status_code < 500:
                pass  # no (usable) robots.txt: RFC 9309 says everything is allowed
            else:  # unreachable / 5xx: be conservative until it can be read
                rules, ttl = RobotsRules(disallow_all=True), ROBOTS_ERROR_TTL_S
                log.warning("robots.txt for %s unavailable; treating as disallow-all", host)
        except ResponseTooLarge:
            rules = RobotsRules(disallow_all=True)
        else:
            if resp.status_code in REDIRECT_STATUSES:  # not followed for robots.txt
                rules = RobotsRules(disallow_all=True)
            else:
                text = resp.content.decode("utf-8", errors="replace")
                rules = parse_robots(text, self.product_token)
        self._robots[host] = (rules, self._clock() + ttl)
        return rules

    # ------------------------------------------------------------------ transport

    async def _get_with_retries(
        self, http: httpx.AsyncClient, method: str, url: str, max_bytes: int, blocked_mode: bool
    ) -> FetchResult:
        attempt = 0
        while True:
            await self._rate_limit()
            try:
                resp = await self._stream_once(http, method, url, max_bytes)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                failure = f"{type(e).__name__}"
                retry_after = None
            else:
                if blocked_mode:
                    self._raise_if_blocked(resp, url)
                if resp.status_code not in RETRY_STATUSES:
                    if resp.status_code >= 400:
                        raise FetchFailed(
                            f"HTTP {resp.status_code} from {_safe_url(url)}", resp.status_code
                        )
                    return resp
                failure = f"HTTP {resp.status_code}"
                retry_after = _retry_after(resp)
            if attempt >= self.max_retries:
                raise FetchFailed(f"{failure} from {_safe_url(url)} after {attempt + 1} attempts")
            delay = min(retry_after, MAX_RETRY_AFTER_S) if retry_after else min(2.0**attempt, 20.0)
            attempt += 1
            log.warning("fetch failed (%s), retry %d in %.1fs", failure, attempt, delay)
            await self._sleep(delay)

    @staticmethod
    def _raise_if_blocked(resp: FetchResult, url: str) -> None:
        """403/429/... and Cloudflare challenges are a "no" from the server: record, don't retry."""
        challenge = resp.headers.get("cf-mitigated", "").lower() == "challenge"
        if resp.status_code in BLOCK_STATUSES or challenge:
            reason = "cloudflare_challenge" if challenge else f"http_{resp.status_code}"
            raise FetchBlocked(
                reason, f"blocked (HTTP {resp.status_code}) at {_safe_url(url)}", resp.status_code
            )
        if resp.status_code == 429:
            raise FetchBlocked(
                "rate_limited_429", f"rate limited (HTTP 429) at {_safe_url(url)}", 429
            )

    async def _stream_once(
        self, http: httpx.AsyncClient, method: str, url: str, max_bytes: int
    ) -> FetchResult:
        async with http.stream(method, url) as r:
            if r.status_code in REDIRECT_STATUSES or r.status_code >= 300 or method == "HEAD":
                # Never read bodies of redirects/errors/HEAD: we don't need them.
                if method == "HEAD":
                    log.info("head %s status=%d", _safe_url(url), r.status_code)
                return FetchResult(url, r.status_code, b"", r.headers)
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLarge(f"Content-Length {declared} exceeds cap")
            buf = bytearray()
            # aiter_bytes yields DECODED bytes, so a compression bomb is capped too.
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    buf.clear()  # discard immediately; leaving the context closes the stream
                    raise ResponseTooLarge(f"response exceeded {max_bytes} bytes")
            log.info("fetched %s status=%d bytes=%d", _safe_url(url), r.status_code, len(buf))
            return FetchResult(url, r.status_code, bytes(buf), r.headers)


def _retry_after(resp: FetchResult) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw and raw.strip().isdigit():
        return float(raw.strip())
    return None
