"""Hardened HTTP client for crtc.gc.ca.

Security properties (each is covered by a test in tests/test_crtc_client.py):

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

log = logging.getLogger(__name__)

# httpx logs every request URL (with query string) at INFO. Keep it out of our logs; we log
# a sanitised line ourselves.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

ALLOWED_DOMAIN = "crtc.gc.ca"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 30.0

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
        max_redirects: int | None = None,
        max_retries: int | None = None,
        min_interval_s: float | None = None,
        connect_timeout_s: float | None = None,
        read_timeout_s: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        s = get_settings()
        self.max_response_bytes = max_response_bytes or s.crtc_max_response_bytes
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

    async def fetch(self, url: str) -> FetchResult:
        """GET `url`, following validated redirects. Raises CrtcClientError subclasses."""
        async with self._new_http_client() as http:
            current = url
            for hop in range(self.max_redirects + 1):
                await self._validate(current)
                resp = await self._get_with_retries(http, current)
                if resp.status_code in REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchFailed("redirect without Location header")
                    nxt = urljoin(current, location)
                    log.info("redirect %s -> %s", _safe_url(current), _safe_url(nxt))
                    current = nxt  # validated at top of the next iteration
                    continue
                if not 200 <= resp.status_code < 300:
                    raise FetchFailed(f"unexpected HTTP {resp.status_code}")
                return resp
            raise TooManyRedirects(f"more than {self.max_redirects} redirects")

    async def _get_with_retries(self, http: httpx.AsyncClient, url: str) -> FetchResult:
        attempt = 0
        while True:
            await self._rate_limit()
            try:
                resp = await self._stream_once(http, url)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                failure = f"{type(e).__name__}"
                retry_after = None
            else:
                if resp.status_code not in RETRY_STATUSES:
                    if resp.status_code >= 400:
                        raise FetchFailed(f"HTTP {resp.status_code} from {_safe_url(url)}")
                    return resp
                failure = f"HTTP {resp.status_code}"
                retry_after = _retry_after(resp)
            if attempt >= self.max_retries:
                raise FetchFailed(f"{failure} from {_safe_url(url)} after {attempt + 1} attempts")
            delay = min(retry_after, MAX_RETRY_AFTER_S) if retry_after else min(2.0**attempt, 20.0)
            attempt += 1
            log.warning("fetch failed (%s), retry %d in %.1fs", failure, attempt, delay)
            await self._sleep(delay)

    async def _stream_once(self, http: httpx.AsyncClient, url: str) -> FetchResult:
        async with http.stream("GET", url) as r:
            if r.status_code in REDIRECT_STATUSES or r.status_code >= 300:
                # Never read bodies of redirects/errors: we don't need them.
                return FetchResult(url, r.status_code, b"", r.headers)
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
                raise ResponseTooLarge(f"Content-Length {declared} exceeds cap")
            buf = bytearray()
            # aiter_bytes yields DECODED bytes, so a compression bomb is capped too.
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > self.max_response_bytes:
                    buf.clear()  # discard immediately; leaving the context closes the stream
                    raise ResponseTooLarge(f"response exceeded {self.max_response_bytes} bytes")
            log.info("fetched %s status=%d bytes=%d", _safe_url(url), r.status_code, len(buf))
            return FetchResult(url, r.status_code, bytes(buf), r.headers)


def _retry_after(resp: FetchResult) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw and raw.strip().isdigit():
        return float(raw.strip())
    return None
